"""Tests for onboarding and experience mode."""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from resume_app.experience import (
    get_post_login_url,
    has_bound_search_profile,
    has_my_jobs_search_profile,
    is_power_user,
    needs_onboarding,
    set_experience_mode,
)
from resume_app.models import Track, UserExperienceSettings, UserResume
from resume_app.onboarding import seed_user_defaults
from resume_app.saved_searches import create_or_update_saved_search
from resume_app.test_utils import TEST_PASSWORD, TENANT_TEST_MIDDLEWARE, login_client

User = get_user_model()


def _attach_library_resume(user, name: str = "resume.pdf") -> UserResume:
    pdf = SimpleUploadedFile(name, b"%PDF-1.4 test", content_type="application/pdf")
    return UserResume.objects.create(
        owner=user,
        file=pdf,
        original_filename=name,
        is_library=True,
    )


@override_settings(MIDDLEWARE=TENANT_TEST_MIDDLEWARE, DEFAULT_EXPERIENCE_MODE="normal")
class OnboardingExperienceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="seeker", password=TEST_PASSWORD, email="seeker@test.com")
        seed_user_defaults(self.user)

    def test_new_normal_user_needs_onboarding(self):
        self.assertTrue(needs_onboarding(self.user))
        self.assertFalse(is_power_user(self.user))
        self.assertEqual(get_post_login_url(self.user), reverse("career_profiler"))

    def test_power_user_skips_onboarding(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        set_experience_mode(self.user, UserExperienceSettings.ExperienceMode.POWER)
        self.assertTrue(is_power_user(self.user))
        self.assertFalse(needs_onboarding(self.user))
        self.assertEqual(get_post_login_url(self.user), reverse("career_cockpit"))

    def test_non_staff_cannot_become_power_user(self):
        set_experience_mode(self.user, UserExperienceSettings.ExperienceMode.POWER)
        self.assertFalse(is_power_user(self.user))
        exp = UserExperienceSettings.get_for_user(self.user)
        self.assertEqual(exp.experience_mode, UserExperienceSettings.ExperienceMode.NORMAL)

    def test_login_redirects_to_profiler(self):
        login_client(self.client, self.user)
        response = self.client.get(reverse("landing"), follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("career_profiler"))

    def test_getting_started_redirects_to_profiler(self):
        login_client(self.client, self.user)
        response = self.client.get(reverse("getting_started"), follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("career_profiler"))

    @override_settings(OPENAI_API_KEY="sk-test-key")
    def test_server_llm_keys_available_without_byok(self):
        from resume_app.experience import llm_ready_for_user, onboarding_progress

        login_client(self.client, self.user)
        self.assertTrue(llm_ready_for_user(self.user))
        progress = onboarding_progress(self.user)
        self.assertTrue(progress["platform_llm"])
        self.assertTrue(progress["llm_done"])
        self.assertFalse(progress["show_byok_hint"])

    def test_onboarding_requires_bound_search_profile(self):
        from resume_app.experience import onboarding_progress, onboarding_steps_complete

        resume = _attach_library_resume(self.user)
        self.assertFalse(onboarding_steps_complete(self.user))
        progress = onboarding_progress(self.user)
        self.assertTrue(progress["resume_done"])
        self.assertFalse(progress["search_done"])

        create_or_update_saved_search(
            self.user,
            name="FinCrimes",
            search_term="AML",
            resume_id=resume.id,
        )
        self.assertTrue(has_bound_search_profile(self.user))
        self.assertTrue(onboarding_steps_complete(self.user))
        self.assertFalse(needs_onboarding(self.user))

    def test_cockpit_empty_state_without_search_profile(self):
        self.assertFalse(has_my_jobs_search_profile(self.user))
        login_client(self.client, self.user)
        response = self.client.get(reverse("career_cockpit"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Create your first Search Profile")
        self.assertContains(response, "Find jobs")

    def test_legacy_pipeline_redirects_to_cockpit_preserving_query(self):
        resume = _attach_library_resume(self.user)
        create_or_update_saved_search(
            self.user,
            name="FinCrimes",
            search_term="AML",
            resume_id=resume.id,
        )
        login_client(self.client, self.user)
        response = self.client.get(
            reverse("pipeline") + "?track=fincrimes&tag=remote",
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/jobs/cockpit/", response.url)
        self.assertIn("stage=pipeline", response.url)
        self.assertIn("track=fincrimes", response.url)
        self.assertIn("tag=remote", response.url)

    def test_my_jobs_unlocks_after_saved_search(self):
        resume = _attach_library_resume(self.user)
        create_or_update_saved_search(
            self.user,
            name="FinCrimes",
            search_term="AML",
            resume_id=resume.id,
        )
        self.assertTrue(has_my_jobs_search_profile(self.user))
        login_client(self.client, self.user)
        response = self.client.get(reverse("career_cockpit"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Set up your first search profile")
        self.assertContains(response, "New")

    def test_saved_search_requires_resume_for_normal_users(self):
        with self.assertRaises(ValueError):
            create_or_update_saved_search(self.user, name="NoResume", search_term="Python")

    def test_my_jobs_search_profiles_match_saved_searches_only(self):
        resume = _attach_library_resume(self.user)
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
        create_or_update_saved_search(
            self.user,
            name="FinCrimes",
            search_term="AML",
            resume_id=resume.id,
        )
        create_or_update_saved_search(
            self.user,
            name="PE",
            search_term="Private equity",
            resume_id=resume.id,
        )
        login_client(self.client, self.user)
        response = self.client.get(reverse("career_cockpit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FinCrimes")
        self.assertContains(response, "PE")
        self.assertNotContains(response, "IC (Principal")
        self.assertNotContains(response, "Management (Manager")

    def test_normal_nav_and_find_jobs_link_to_search_profiles(self):
        resume = _attach_library_resume(self.user)
        create_or_update_saved_search(
            self.user,
            name="FinCrimes",
            search_term="AML",
            resume_id=resume.id,
        )
        login_client(self.client, self.user)
        search = self.client.get(reverse("jobs_search"))
        self.assertEqual(search.status_code, 200)
        self.assertContains(search, 'href="' + reverse("career_cockpit") + '"')
        self.assertContains(search, "Search Profiles")
        self.assertContains(search, "Manage in Cockpit")
        tracks = self.client.get(reverse("track_list"))
        self.assertEqual(tracks.status_code, 302)
        self.assertIn("/jobs/cockpit/", tracks.url)
