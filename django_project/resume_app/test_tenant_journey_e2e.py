import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from resume_app.models import (
    JobListing,
    JobListingAction,
    PipelineEntry,
    SavedJobSearch,
    SearchProfile,
    Track,
)

User = get_user_model()


class TenantJourneyE2ETest(TestCase):
    def setUp(self):
        self.password = "TestPass123!"
        self.tenant_user = User.objects.create_user(
            username="tenant_tester",
            email="tenant_tester@example.com",
            password=self.password,
            is_staff=False,
            is_superuser=False,
        )
        self.staff_user = User.objects.create_user(
            username="staff_tester",
            email="staff_tester@example.com",
            password=self.password,
            is_staff=True,
            is_superuser=True,
        )
        self.track = Track.objects.create(
            owner=self.tenant_user,
            label="Senior Engineering",
            slug="eng-lead",
        )
        self.job = JobListing.objects.create(
            title="Senior Python Architect",
            company_name="CloudScale Corp",
            location="Remote",
            description="Build scalable distributed services with Python and Django.",
            url="https://example.com/job/1",
            source="indeed",
        )
        self.preset = SearchProfile.objects.create(
            owner=self.tenant_user,
            name="Remote Python Leads",
            slug="py-lead",
            search_term="Python Lead",
            location="Remote",
            profile_slug="eng-lead",
        )
        self.client = Client()

    @patch("resume_app.job_search_core.fetch_jobs", return_value=[])
    def test_navigation_3_tab_model_for_tenant(self, mock_fetch):
        self.client.login(username="tenant_tester", password=self.password)

        # 1. Target & Discover: AI Profiler
        resp = self.client.get(reverse("career_profiler"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Target &amp; Discover")
        self.assertContains(resp, "AI Resume Profiler")
        self.assertContains(resp, "Search Sandbox")
        # Ensure Tools menu does not expose Workflow to non-staff tenant
        self.assertNotContains(resp, "href=\"/resume/workflow/\"")

        # 2. Target & Discover: Search Sandbox (Default view)
        resp = self.client.get(reverse("jobs_search"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Target &amp; Discover")
        self.assertContains(resp, "Search Sandbox")
        self.assertContains(resp, "Save Query to Cockpit")

        # 3. Target & Discover: Search Sandbox (With preset selected)
        resp_preset = self.client.get(f"{reverse('jobs_search')}?preset={self.preset.id}")
        self.assertEqual(resp_preset.status_code, 200)
        self.assertContains(resp_preset, "Automate into Cockpit")
        self.assertContains(resp_preset, "Save Cockpit Schedule")

    def test_sandbox_to_cockpit_save_bridge(self):
        self.client.login(username="tenant_tester", password=self.password)

        # Save job from Search Sandbox to Cockpit (Ninja router expects track as query param)
        save_url = f"/api/resume/jobs/{self.job.id}/save?track=py-lead"
        resp = self.client.post(
            save_url,
            content_type="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)

        # Confirm PipelineEntry was created for tenant
        entry = PipelineEntry.objects.filter(
            owner=self.tenant_user,
            job_listing=self.job,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.track, "py-lead")
        self.assertEqual(entry.stage, "vetting")

        # Confirm the job appears in Cockpit Review stage
        resp_cockpit = self.client.get(f"{reverse('career_cockpit')}?track=py-lead&stage=vetting")
        self.assertEqual(resp_cockpit.status_code, 200)
        self.assertContains(resp_cockpit, "Senior Python Architect")
        self.assertContains(resp_cockpit, "CloudScale Corp")
        # Confirm slide-over drawer markup exists
        self.assertContains(resp_cockpit, "automation-drawer-backdrop")
        self.assertContains(resp_cockpit, "openAutomationDrawer()")

    def test_day_1_empty_state_onboarding_checklist(self):
        self.client.login(username="tenant_tester", password=self.password)

        # For an empty track with 0 jobs, Cockpit must show Day-1 3-step onboarding checklist
        resp = self.client.get(f"{reverse('career_cockpit')}?track=empty-track&stage=pipeline")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Get Started in 3 Simple Steps")
        self.assertContains(resp, "Define Target &amp; Profile")
        self.assertContains(resp, "Discover &amp; Ingest Roles")
        self.assertContains(resp, "Triage, Tailor &amp; Track")
        self.assertContains(resp, "Find Jobs in Sandbox")

    def test_tracker_positive_momentum(self):
        self.client.login(username="tenant_tester", password=self.password)

        resp = self.client.get(reverse("performance_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Career Momentum &amp; Outcomes")
        self.assertContains(resp, "Weekly Goal Progress")
        self.assertContains(resp, "Active Streak")
        # Ensure punitive warnings are absent
        self.assertNotContains(resp, "Behind Weekly Pace")
        self.assertNotContains(resp, "Pacing Deficit")

    def test_app_settings_hidden_from_tenants(self):
        # Tenant should not see App Automation settings tab
        self.client.login(username="tenant_tester", password=self.password)
        resp = self.client.get(reverse("settings"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "?tab=app")

        # Staff user should see App Automation settings tab
        self.client.login(username="staff_tester", password=self.password)
        resp_staff = self.client.get(reverse("settings"))
        self.assertEqual(resp_staff.status_code, 200)
        self.assertContains(resp_staff, "?tab=app")
