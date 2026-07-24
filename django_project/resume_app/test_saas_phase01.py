"""Phase 0 tenancy isolation and Phase 1 account lifecycle tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from resume_app.account import (
    build_account_export,
    email_taken,
    is_email_verified,
    make_verify_uid_token,
    mark_email_verified,
    normalize_email,
)
from resume_app.crypto import encrypt_api_key
from resume_app.job_prep import _library_resume_text, resolve_interview_prep_inputs
from resume_app.media_access import user_may_access_media
from resume_app.models import (
    ApplicationAttempt,
    JobListing,
    LLMProviderConfig,
    LLMProviderPreference,
    PipelineEntry,
    Track,
    UserExperienceSettings,
    UserResume,
)
from resume_app.pipeline_llm_skill_extract import resolve_provider_api_key
from resume_app.test_utils import TEST_PASSWORD, create_user, login_client


class Phase0IsolationTests(TestCase):
    def setUp(self):
        self.alice = create_user("alice_p0")
        self.bob = create_user("bob_p0")
        Track.ensure_baseline(self.alice)
        Track.ensure_baseline(self.bob)

    def test_resolve_provider_api_key_is_owner_scoped(self):
        LLMProviderConfig.objects.create(
            owner=self.bob,
            provider="OpenAI",
            encrypted_api_key=encrypt_api_key("sk-bob-secret"),
        )
        with override_settings(OPENAI_API_KEY=None):
            self.assertIsNone(resolve_provider_api_key("OpenAI", user=self.alice))
            self.assertEqual(resolve_provider_api_key("OpenAI", user=self.bob), "sk-bob-secret")

    def test_library_resume_fallback_is_owner_scoped(self):
        UserResume.objects.create(
            owner=self.bob,
            original_filename="bob.pdf",
            is_library=True,
            track="ic",
            file=SimpleUploadedFile("bob.pdf", b"%PDF-1.4 bob"),
        )
        with patch("resume_app.job_prep.parse_pdf", return_value="BOB RESUME TEXT"):
            text = _library_resume_text("ic", user=self.alice)
        self.assertEqual(text, "")

        with patch("resume_app.job_prep.parse_pdf", return_value="BOB RESUME TEXT"):
            text_bob = _library_resume_text("ic", user=self.bob)
        self.assertEqual(text_bob, "BOB RESUME TEXT")

    def test_interview_prep_does_not_use_other_user_resume(self):
        job = JobListing.objects.create(
            source="test",
            external_id="prep-1",
            title="Engineer",
            company_name="Acme",
            description="Build things",
        )
        entry = PipelineEntry.objects.create(
            owner=self.alice,
            job_listing=job,
            track="ic",
            stage="pipeline",
        )
        UserResume.objects.create(
            owner=self.bob,
            original_filename="bob.pdf",
            is_library=True,
            track="ic",
            file=SimpleUploadedFile("bob.pdf", b"%PDF-1.4 bob"),
        )
        with patch("resume_app.job_prep.parse_pdf", return_value="SECRET BOB"):
            inputs = resolve_interview_prep_inputs(entry)
        self.assertNotIn("SECRET BOB", inputs.resume_text)

    def test_media_resume_path_requires_owner(self):
        self.assertTrue(user_may_access_media(self.alice, f"resumes/{self.alice.pk}/cv.pdf"))
        self.assertFalse(user_may_access_media(self.alice, f"resumes/{self.bob.pk}/cv.pdf"))

    def test_serve_media_rejects_cross_tenant_resume(self):
        from django.conf import settings

        media_root = Path(settings.MEDIA_ROOT)
        bob_dir = media_root / "resumes" / str(self.bob.pk)
        bob_dir.mkdir(parents=True, exist_ok=True)
        secret = bob_dir / "secret.pdf"
        secret.write_bytes(b"%PDF-1.4 secret")

        client = Client()
        login_client(client, self.alice)
        resp = client.get(f"/media/resumes/{self.bob.pk}/secret.pdf")
        self.assertEqual(resp.status_code, 404)

        login_client(client, self.bob)
        resp_ok = client.get(f"/media/resumes/{self.bob.pk}/secret.pdf")
        self.assertEqual(resp_ok.status_code, 200)

    def test_apply_agent_media_requires_attempt_owner(self):
        job = JobListing.objects.create(
            source="test",
            external_id="apply-media-1",
            title="Role",
            company_name="Co",
        )
        entry = PipelineEntry.objects.create(
            owner=self.bob,
            job_listing=job,
            track="ic",
            stage="applying",
        )
        attempt = ApplicationAttempt.objects.create(pipeline_entry=entry)
        path = f"apply_agent/attempt_{attempt.id}/step.png"
        self.assertFalse(user_may_access_media(self.alice, path))
        self.assertTrue(user_may_access_media(self.bob, path))

    def test_rate_limit_preference_lookup_is_owner_scoped(self):
        from resume_app.llm_rate_limit import get_preference_row_for_provider_model

        cfg_b = LLMProviderConfig.objects.create(
            owner=self.bob,
            provider="Groq",
            encrypted_api_key=encrypt_api_key("g-bob"),
        )
        LLMProviderPreference.objects.create(
            provider_config=cfg_b,
            model="llama",
            priority=1,
            rate_limit_rpm=10,
            rate_limit_tpm=1000,
            rate_limit_cooldown_seconds=99,
        )
        self.assertIsNone(get_preference_row_for_provider_model("Groq", "llama", user=self.alice))
        row = get_preference_row_for_provider_model("Groq", "llama", user=self.bob)
        self.assertIsNotNone(row)
        self.assertEqual(row.rate_limit_cooldown_seconds, 99)

    def test_get_solo_removed(self):
        from resume_app.models import AppAutomationSettings, ApplicantProfile, LLMAppUsageTotals

        self.assertFalse(hasattr(AppAutomationSettings, "get_solo"))
        self.assertFalse(hasattr(ApplicantProfile, "get_solo"))
        self.assertFalse(hasattr(LLMAppUsageTotals, "get_solo"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class Phase1AccountTests(TestCase):
    def setUp(self):
        self.user = create_user("acct_user")
        self.client = Client()

    def test_signup_rejects_duplicate_email(self):
        self.user.email = "dup@example.com"
        self.user.save(update_fields=["email"])
        resp = self.client.post(
            reverse("signup"),
            {
                "username": "otheruser",
                "email": "DUP@example.com",
                "password1": "ComplexPass123!",
                "password2": "ComplexPass123!",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "An account with this email already exists")

    def test_signup_sends_verification_email(self):
        resp = self.client.post(
            reverse("signup"),
            {
                "username": "newbie",
                "email": "newbie@example.com",
                "password1": "ComplexPass123!",
                "password2": "ComplexPass123!",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Verify", mail.outbox[0].subject)

    def test_verify_email_link(self):
        self.user.email = "verifyme@example.com"
        self.user.save(update_fields=["email"])
        exp = UserExperienceSettings.get_for_user(self.user)
        exp.email_verified_at = None
        exp.save(update_fields=["email_verified_at"])
        uid, token = make_verify_uid_token(self.user)
        resp = self.client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(is_email_verified(self.user))

    def test_password_reset_flow_sends_mail(self):
        self.user.email = "reset@example.com"
        self.user.save(update_fields=["email"])
        resp = self.client.post(reverse("password_reset"), {"email": "reset@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    def test_account_export_and_delete(self):
        login_client(self.client, self.user)
        export = build_account_export(self.user)
        self.assertEqual(export["user"]["username"], self.user.username)

        resp = self.client.post(
            reverse("settings") + "?tab=account",
            {"action": "export_account"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        payload = json.loads(resp.content)
        self.assertEqual(payload["user"]["username"], self.user.username)

        username = self.user.get_username()
        resp_del = self.client.post(
            reverse("settings") + "?tab=account",
            {
                "action": "delete_account",
                "confirm_username": username,
                "confirm_password": TEST_PASSWORD,
            },
        )
        self.assertEqual(resp_del.status_code, 302)
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.assertFalse(User.objects.filter(username=username).exists())

    def test_email_helpers(self):
        self.assertEqual(normalize_email("  A@B.COM "), "a@b.com")
        self.user.email = "taken@example.com"
        self.user.save(update_fields=["email"])
        self.assertTrue(email_taken("TAKEN@example.com"))
        mark_email_verified(self.user)
        self.assertTrue(is_email_verified(self.user))

    def test_dedupe_clears_weaker_duplicate_emails(self):
        from django.contrib.auth import get_user_model
        from django.db import connection

        from resume_app.account import dedupe_user_emails
        from resume_app.models import UserResume

        User = get_user_model()
        index_name = "auth_user_email_uniq"

        def drop_index():
            with connection.cursor() as cursor:
                cursor.execute(f"DROP INDEX IF EXISTS {index_name}")

        def create_index():
            with connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                        "ON auth_user (email) WHERE email <> ''"
                    )
                else:
                    cursor.execute(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                        "ON auth_user (email) WHERE email != ''"
                    )

        drop_index()
        try:
            keeper = User.objects.create_user(
                username="keep_me", password="pass12345!", email="same@example.com"
            )
            loser = User.objects.create_user(
                username="lose_me", password="pass12345!", email="same@example.com"
            )
            UserResume.objects.create(owner=keeper, original_filename="k.pdf", is_library=True)
            actions = dedupe_user_emails(dry_run=False)
            keeper.refresh_from_db()
            loser.refresh_from_db()
            self.assertEqual(keeper.email, "same@example.com")
            self.assertEqual(loser.email, "")
            self.assertTrue(any(a.get("action") == "clear_duplicate" for a in actions))
        finally:
            create_index()

    def test_db_rejects_duplicate_nonblank_email(self):
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError, transaction

        User = get_user_model()
        User.objects.create_user(username="first_email", password="pass12345!", email="uniq@example.com")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    username="second_email",
                    password="pass12345!",
                    email="UNIQ@example.com",
                )
