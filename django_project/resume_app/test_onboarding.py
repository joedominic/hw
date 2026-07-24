"""Tests for onboarding and experience mode."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from resume_app.experience import (
    get_post_login_url,
    has_my_jobs_search_profile,
    is_power_user,
    needs_onboarding,
    set_experience_mode,
)
from resume_app.models import SavedJobSearch, Track, UserExperienceSettings, UserResume
from resume_app.onboarding import seed_user_defaults
from resume_app.saved_searches import create_or_update_saved_search
from resume_app.test_utils import TEST_PASSWORD, TENANT_TEST_MIDDLEWARE, login_client

User = get_user_model()


@override_settings(MIDDLEWARE=TENANT_TEST_MIDDLEWARE, DEFAULT_EXPERIENCE_MODE="normal")
class OnboardingExperienceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="seeker", password=TEST_PASSWORD, email="seeker@test.com")
        seed_user_defaults(self.user)

    def test_new_normal_user_needs_onboarding(self):
        self.assertTrue(needs_onboarding(self.user))
        self.assertFalse(is_power_user(self.user))
        self.assertEqual(get_post_login_url(self.user), reverse("getting_started"))

    def test_power_user_skips_onboarding(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        set_experience_mode(self.user, UserExperienceSettings.ExperienceMode.POWER)
        self.assertTrue(is_power_user(self.user))
        self.assertFalse(needs_onboarding(self.user))
        self.assertEqual(get_post_login_url(self.user), reverse("pipeline"))

    def test_non_staff_cannot_become_power_user(self):
        set_experience_mode(self.user, UserExperienceSettings.ExperienceMode.POWER)
        self.assertFalse(is_power_user(self.user))
        exp = UserExperienceSettings.get_for_user(self.user)
        self.assertEqual(exp.experience_mode, UserExperienceSettings.ExperienceMode.NORMAL)

    def test_login_redirects_to_getting_started(self):
        login_client(self.client, self.user)
        response = self.client.get(reverse("landing"), follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("getting_started"))

    def test_resume_upload_completes_step(self):
        login_client(self.client, self.user)
        from django.core.files.uploadedfile import SimpleUploadedFile

        pdf = SimpleUploadedFile("resume.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        response = self.client.post(
            reverse("getting_started"),
            {"action": "upload_resume", "resume_file": pdf},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(UserResume.objects.filter(owner=self.user, is_library=True).exists())
        exp = UserExperienceSettings.get_for_user(self.user)
        self.assertTrue(exp.step_resume_uploaded)

    def test_dismiss_onboarding_goes_to_pipeline(self):
        login_client(self.client, self.user)
        response = self.client.post(reverse("getting_started"), {"action": "dismiss"})
        self.assertRedirects(response, reverse("pipeline"))
        exp = UserExperienceSettings.get_for_user(self.user)
        self.assertIsNotNone(exp.onboarding_dismissed_at)

    @override_settings(OPENAI_API_KEY="sk-test-key")
    def test_server_llm_keys_available_without_byok(self):
        from resume_app.experience import llm_ready_for_user, onboarding_progress

        login_client(self.client, self.user)
        self.assertTrue(llm_ready_for_user(self.user))
        progress = onboarding_progress(self.user)
        self.assertTrue(progress["platform_llm"])
        self.assertTrue(progress["llm_done"])
        self.assertFalse(progress["show_byok_hint"])

    def test_onboarding_complete_without_user_api_key(self):
        from resume_app.experience import onboarding_progress, onboarding_steps_complete
        from django.core.files.uploadedfile import SimpleUploadedFile

        login_client(self.client, self.user)
        pdf = SimpleUploadedFile("resume.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        self.client.post(reverse("getting_started"), {"action": "upload_resume", "resume_file": pdf})
        self.assertFalse(onboarding_steps_complete(self.user))
        progress = onboarding_progress(self.user)
        self.assertTrue(progress["resume_done"])
        self.assertFalse(progress["search_done"])

    def test_my_jobs_gate_without_search_profile(self):
        self.assertFalse(has_my_jobs_search_profile(self.user))
        login_client(self.client, self.user)
        response = self.client.get(reverse("pipeline"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Set up your first search profile")
        self.assertContains(response, "Go to Find jobs")
        self.assertNotContains(response, "Shortlist")

    def test_my_jobs_unlocks_after_saved_search(self):
        create_or_update_saved_search(self.user, name="FinCrimes", search_term="AML")
        self.assertTrue(has_my_jobs_search_profile(self.user))
        login_client(self.client, self.user)
        response = self.client.get(reverse("pipeline"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Set up your first search profile")
        self.assertContains(response, "New")

    def test_my_jobs_search_profiles_match_saved_searches_only(self):
        Track.objects.create(
            owner=self.user,
            slug="ic",
            label="IC (Principal / Staff)",
        )
        Track.objects.create(
            owner=self.user,
            slug="mgmt",
            label="Management (Manager / Director)",
        )
        create_or_update_saved_search(self.user, name="FinCrimes", search_term="AML")
        create_or_update_saved_search(self.user, name="PE", search_term="Private equity")
        login_client(self.client, self.user)
        response = self.client.get(reverse("pipeline"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FinCrimes")
        self.assertContains(response, "PE")
        self.assertNotContains(response, "IC (Principal")
        self.assertNotContains(response, "Management (Manager")

    def test_normal_nav_and_find_jobs_link_to_search_profiles(self):
        login_client(self.client, self.user)
        self.client.post(reverse("getting_started"), {"action": "dismiss"})
        search = self.client.get(reverse("jobs_search"))
        self.assertEqual(search.status_code, 200)
        self.assertContains(search, 'href="' + reverse("track_list") + '"')
        self.assertContains(search, "Search profiles")
        self.assertContains(search, "Open Search profiles")
        tracks = self.client.get(reverse("track_list"))
        self.assertEqual(tracks.status_code, 200)
        self.assertContains(tracks, "assign it to a search profile")
        self.assertNotContains(tracks, "Jobs workspace")
        self.assertNotContains(tracks, ">Searches</a>")
