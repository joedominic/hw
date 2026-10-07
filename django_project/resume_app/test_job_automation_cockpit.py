import time
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, Client
from django.urls import reverse

from resume_app.models import (
    JobDescription,
    JobListing,
    JobListingAction,
    JobListingEmbedding,
    JobListingTrackMetrics,
    JobSearchTask,
    JobSearchTaskRun,
    OptimizedResume,
    PipelineEntry,
    SearchProfile,
    Track,
    UserResume,
)


class JobAutomationCockpitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="testuser",
            password="password123",
            email="test@example.com",
        )
        self.client = Client()
        self.client.login(username="testuser", password="password123")

        self.profile = SearchProfile.objects.create(
            owner=self.user,
            name="Senior Python Dev",
            slug="senior-python",
            search_term="Senior Python Developer",
            location="Remote",
            is_default=True,
        )

        self.task = JobSearchTask.objects.create(
            owner=self.user,
            saved_search=self.profile,
            name="Senior Python Dev",
            search_term="Senior Python Developer",
            track="senior-python",
            frequency="0 9 * * *",
            is_active=True,
        )

    def tearDown(self):
        cache.clear()

    def test_job_automation_redirects_to_career_cockpit(self):
        """Legacy /jobs/automation/ must redirect to /jobs/cockpit/."""
        response = self.client.get(reverse("job_automation"))
        self.assertRedirects(response, reverse("career_cockpit"), fetch_redirect_response=False)

        response_track = self.client.get(reverse("job_automation") + "?track=senior-python")
        self.assertRedirects(response_track, reverse("career_cockpit") + "?track=senior-python", fetch_redirect_response=False)

    def test_job_search_task_run_jobs_eliminated_property(self):
        """Test jobs_eliminated calculation on JobSearchTaskRun."""
        run = JobSearchTaskRun.objects.create(
            task=self.task,
            status=JobSearchTaskRun.STATUS_COMPLETED,
            jobs_fetched=100,
            jobs_after_filter=40,
            jobs_added_to_pipeline=25,
        )
        self.assertEqual(run.jobs_eliminated, 60)

    @patch("resume_app.views.run_job_search_task")
    def test_job_task_run_now_cooldown_enforcement(self, mock_run_task):
        """Verify on-demand run enforces 60-min cooldown between manual triggers."""
        run_url = reverse("job_task_run_now", args=[self.task.id])

        # 1. First trigger succeeds
        res1 = self.client.post(run_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1["status"], "queued")
        self.assertEqual(data1["cooldown_remaining_seconds"], 3600)
        mock_run_task.assert_called_once_with(self.user.id, self.task.id)

        # 2. Immediate second trigger is throttled with 429
        mock_run_task.reset_mock()
        res2 = self.client.post(run_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res2.status_code, 429)
        data2 = res2.json()
        self.assertEqual(data2["status"], "cooldown")
        self.assertTrue(data2["cooldown_remaining_seconds"] > 3500)
        mock_run_task.assert_not_called()

    @patch("resume_app.views.run_job_search_task")
    def test_job_task_run_now_concurrency_guard(self, mock_run_task):
        """Verify cannot trigger if task is actively running."""
        from resume_app.tasks import get_job_search_task_lock_key
        lock_key = get_job_search_task_lock_key(self.user.id)
        cache.set(lock_key, 1, 600)

        run_url = reverse("job_task_run_now", args=[self.task.id])
        res = self.client.post(run_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res.status_code, 409)
        data = res.json()
        self.assertEqual(data["status"], "running")
        mock_run_task.assert_not_called()

    def test_job_task_status_endpoint(self):
        """Verify job_task_status endpoint returns live status, last run, and cooldown."""
        status_url = reverse("job_task_status", args=[self.task.id])

        # Initially no runs, not running, no cooldown
        res1 = self.client.get(status_url)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1["status"], "ok")
        self.assertFalse(data1["is_running"])
        self.assertEqual(data1["cooldown_remaining_seconds"], 0)
        self.assertIsNone(data1["last_run"])

        # Add a completed run
        run = JobSearchTaskRun.objects.create(
            task=self.task,
            status=JobSearchTaskRun.STATUS_COMPLETED,
            jobs_fetched=80,
            jobs_after_filter=20,
            jobs_added_to_pipeline=20,
            details=[{"title": "Job A"}],
        )
        # Set cooldown
        cache.set(f"job_task_manual_cooldown:{self.task.id}", time.time() + 1800, timeout=1800)

        res2 = self.client.get(status_url)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertFalse(data2["is_running"])
        self.assertTrue(1750 <= data2["cooldown_remaining_seconds"] <= 1800)
        self.assertIsNotNone(data2["last_run"])
        self.assertEqual(data2["last_run"]["id"], run.id)
        self.assertEqual(data2["last_run"]["jobs_fetched"], 80)
        self.assertEqual(data2["last_run"]["jobs_added"], 20)
        self.assertEqual(data2["last_run"]["jobs_eliminated"], 60)
        self.assertTrue(data2["last_run"]["has_details"])

    def test_delete_search_profile_and_associated_data_from_cockpit(self):
        """Verify deleting a search profile deletes all associated tasks, pipeline, actions, embeddings, and metrics."""
        # Create a second profile so there are multiple
        profile2 = SearchProfile.objects.create(
            owner=self.user,
            name="Data Engineer",
            slug="data-eng",
            search_term="Data Engineer",
            location="Remote",
            is_default=False,
        )
        task2 = JobSearchTask.objects.create(
            owner=self.user,
            saved_search=profile2,
            name="Data Engineer",
            search_term="Data Engineer",
            track="data-eng",
            frequency="0 9 * * *",
        )
        job = JobListing.objects.create(title="Data Scientist", company_name="AI Corp")
        pe = PipelineEntry.objects.create(owner=self.user, job_listing=job, track="data-eng", search_profile=profile2)
        action = JobListingAction.objects.create(
            owner=self.user,
            job_listing=job,
            action=JobListingAction.ActionType.LIKED,
            track="data-eng",
            search_profile=profile2,
        )
        emb = JobListingEmbedding.objects.create(
            owner=self.user,
            job_listing=job,
            embedding_type=JobListingEmbedding.EmbeddingType.LIKED,
            track="data-eng",
            search_profile=profile2,
            embedding=[0.1] * 384,
        )
        metric = JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="data-eng",
            search_profile=profile2,
            focus_percent=85,
        )
        track_obj = Track.objects.create(owner=self.user, slug="data-eng", label="Data Engineer")

        # Submit cockpit POST to delete profile2
        cockpit_url = reverse("career_cockpit")
        response = self.client.post(
            cockpit_url,
            {
                "action": "delete_search_profile",
                "delete_profile_id": str(profile2.id),
                "delete_profile_slug": "data-eng",
            },
        )
        self.assertEqual(response.status_code, 302)

        # Assert profile and all associated data are wiped
        self.assertFalse(SearchProfile.objects.filter(id=profile2.id).exists())
        self.assertFalse(JobSearchTask.objects.filter(id=task2.id).exists())
        self.assertFalse(PipelineEntry.objects.filter(id=pe.id).exists())
        self.assertFalse(JobListingAction.objects.filter(id=action.id).exists())
        self.assertFalse(JobListingEmbedding.objects.filter(id=emb.id).exists())
        self.assertFalse(JobListingTrackMetrics.objects.filter(id=metric.id).exists())
        self.assertFalse(Track.objects.filter(id=track_obj.id).exists())

        # Remaining profile is still intact
        self.assertTrue(SearchProfile.objects.filter(id=self.profile.id).exists())

    def test_delete_only_remaining_search_profile_resets_to_baseline(self):
        """Verify deleting the user's only profile wipes all data and initializes a fresh baseline profile."""
        cockpit_url = reverse("career_cockpit")
        response = self.client.post(
            cockpit_url,
            {
                "action": "delete_search_profile",
                "delete_profile_id": str(self.profile.id),
            },
        )
        self.assertEqual(response.status_code, 302)

        # Original profile deleted
        self.assertFalse(SearchProfile.objects.filter(id=self.profile.id).exists())
        # Fresh baseline profile created
        remaining = SearchProfile.objects.filter(owner=self.user)
        self.assertEqual(remaining.count(), 1)
        new_default = remaining.first()
        self.assertEqual(new_default.slug, "general")
        self.assertTrue(new_default.is_default)

    def test_mark_done_snapshots_tailored_resume_markdown(self):
        """When moving to Applied, PipelineEntry snapshots tailored resume markdown if one exists."""
        job = JobListing.objects.create(
            title="Senior Staff Engineer",
            company_name="Acme Corp",
            location="Remote",
            url="https://example.com/job/101",
        )
        entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="senior-python",
            stage=PipelineEntry.Stage.APPLYING,
        )
        jd = JobDescription.objects.create(content="Job description text")
        ur = UserResume.objects.create(owner=self.user, original_filename="base.pdf")
        opt = OptimizedResume.objects.create(
            owner=self.user,
            original_resume=ur,
            job_description=jd,
            pipeline_entry=entry,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="# Jane Doe\n\n## Experience\n- Lead Engineer at TechCorp",
        )

        entry.mark_done()
        entry.refresh_from_db()

        self.assertEqual(entry.stage, PipelineEntry.Stage.DONE)
        self.assertIsNotNone(entry.applied_at)
        self.assertEqual(entry.applied_resume_markdown, opt.optimized_content)
        self.assertEqual(entry.applied_optimized_resume_id, opt.id)

    def test_mark_done_leaves_empty_if_no_tailored_resume(self):
        """When moving to Applied without a tailored resume, applied_resume_markdown is left empty."""
        job = JobListing.objects.create(
            title="Backend Architect",
            company_name="Globex",
            location="San Francisco, CA",
            url="https://example.com/job/102",
        )
        entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="senior-python",
            stage=PipelineEntry.Stage.APPLYING,
        )

        entry.mark_done()
        entry.refresh_from_db()

        self.assertEqual(entry.stage, PipelineEntry.Stage.DONE)
        self.assertEqual(entry.applied_resume_markdown, "")
        self.assertIsNone(entry.applied_optimized_resume)

    def test_pipeline_applied_resume_json_view(self):
        """Test API endpoint returning applied resume JSON snapshot."""
        job = JobListing.objects.create(
            title="Principal Engineer",
            company_name="Starlight Corp",
            location="Remote",
            url="https://example.com/job/103",
        )
        entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="senior-python",
            stage=PipelineEntry.Stage.DONE,
            applied_resume_markdown="# Resume for Starlight\n\n- Built scalable systems",
        )

        # 1. Successful fetch with resume
        url = reverse("pipeline_applied_resume_json", args=[entry.id])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["has_resume"])
        self.assertEqual(data["job_title"], "Principal Engineer")
        self.assertEqual(data["company_name"], "Starlight Corp")
        self.assertIn("Built scalable systems", data["markdown"])

        # 2. Fetch for entry without resume
        entry2 = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="general",
            stage=PipelineEntry.Stage.DONE,
            applied_resume_markdown="",
        )
        res2 = self.client.get(reverse("pipeline_applied_resume_json", args=[entry2.id]))
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2["success"])
        self.assertFalse(data2["has_resume"])
        self.assertEqual(data2["markdown"], "")

    def test_pipeline_export_pdf_and_docx(self):
        """Test PDF and Word export endpoints for an applied pipeline entry."""
        job = JobListing.objects.create(
            title="Lead Architect",
            company_name="Wayne Enterprises",
            location="Gotham",
            url="https://example.com/job/104",
        )
        entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="senior-python",
            stage=PipelineEntry.Stage.DONE,
            applied_resume_markdown="# Bruce Wayne\n\n## Summary\nExperienced vigilante and architect.",
        )

        # Test PDF export
        pdf_url = reverse("pipeline_export_pdf", args=[entry.id])
        pdf_res = self.client.get(pdf_url)
        self.assertEqual(pdf_res.status_code, 200)
        self.assertEqual(pdf_res["Content-Type"], "application/pdf")
        self.assertIn("attachment;", pdf_res["Content-Disposition"])

        # Test Word export
        docx_url = reverse("pipeline_export_docx", args=[entry.id])
        docx_res = self.client.get(docx_url)
        self.assertEqual(docx_res.status_code, 200)
        self.assertIn("wordprocessingml.document", docx_res["Content-Type"])
        self.assertIn("attachment;", docx_res["Content-Disposition"])

        # Test 404 when markdown is empty
        empty_entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="general",
            stage=PipelineEntry.Stage.DONE,
            applied_resume_markdown="",
        )
        empty_pdf_res = self.client.get(reverse("pipeline_export_pdf", args=[empty_entry.id]))
        self.assertEqual(empty_pdf_res.status_code, 404)

    def test_career_cockpit_applying_and_done_stages_render_without_schema_error(self):
        """Regression test: loading career cockpit in applying and done stages must not fail with JobPayload schema errors."""
        job1 = JobListing.objects.create(
            source="test",
            external_id="ext-105",
            title="Enterprise Architect",
            company_name="MegaCorp",
            location="Remote",
            url="https://example.com/job/105",
        )
        job2 = JobListing.objects.create(
            source="test",
            external_id="ext-106",
            title="Solutions Architect",
            company_name="GigaCorp",
            location="Remote",
            url="https://example.com/job/106",
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job1,
            track="senior-python",
            stage=PipelineEntry.Stage.APPLYING,
        )
        PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job2,
            track="senior-python",
            stage=PipelineEntry.Stage.DONE,
            applied_resume_markdown="# Tailored Markdown",
        )

        # 1. Stage applying
        res_applying = self.client.get(reverse("career_cockpit") + "?track=senior-python&stage=applying")
        self.assertEqual(res_applying.status_code, 200)

        # 2. Stage done
        res_done = self.client.get(reverse("career_cockpit") + "?track=senior-python&stage=done")
        self.assertEqual(res_done.status_code, 200)
        self.assertContains(res_done, "Submitted Resume")


