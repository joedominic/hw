"""
Unit tests verifying zero-data dashboard truth-in-advertising, fail-closed production safety,
and workflow state integrity.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.core.exceptions import ImproperlyConfigured

from resume_app.dashboard_stats import get_performance_dashboard_stats
from resume_app.models import JobListing, PipelineEntry, SearchProfile, Track
from resume_app.search_profile_scope import (
    ensure_pipeline_entry_on_manual_save,
    remove_pipeline_entry_on_unsave,
)

User = get_user_model()


class ZeroDataDashboardStatsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="zerouser@example.com",
            email="zerouser@example.com",
            password="test-password-123",
        )

    def test_new_account_displays_no_fabricated_nonzero_metrics(self):
        """A new account must display clean zero states with no invented numbers."""
        stats = get_performance_dashboard_stats(self.user)

        # Streak must be 0 (no fake 19-day streak)
        self.assertEqual(stats["streak_days"], 0)

        # Scanned listings must match DB count (0, not 55,088)
        self.assertEqual(stats["roles_scanned"], "0")
        self.assertEqual(stats["scanned_this_week"], 0)

        # High affinity roles must be 0 (not 76)
        self.assertEqual(stats["high_affinity_roles"], 0)

        # Pipeline counts must be 0 (not 46 or 19)
        self.assertEqual(stats["total_in_pipeline"], 0)
        self.assertEqual(stats["total_applied"], 0)
        self.assertEqual(stats["applied_this_week"], 0)
        self.assertEqual(stats["ready_queue_count"], 0)

        # Valuation must be $0 (not $11.3M)
        self.assertEqual(stats["pipeline_val_display"], "$0")

        # Funnel stages must not report fake 18,720 vetted or 100% valid
        funnel = stats["funnel_stages"]
        self.assertEqual(funnel[0]["count"], "0")  # Discovered
        self.assertEqual(funnel[1]["count"], "0")  # Vetted
        self.assertEqual(funnel[2]["count"], "0")  # Shortlisted
        self.assertEqual(funnel[3]["count"], "0")  # Ready queue
        self.assertEqual(funnel[4]["count"], "0")  # Applied
        self.assertNotIn("100% Valid", funnel[4]["rate_label"])

        # ROI & Time Saved must be 0 (not 18.4 Hours or ~$4,200)
        self.assertEqual(stats["executive_hours_saved"], "0 Hours")
        self.assertEqual(stats["executive_value_preserved"], "$0")
        self.assertNotIn("4.2 min", stats["executive_turnaround"])
        self.assertEqual(stats["truth_verified"], "—")
        self.assertEqual(stats["score_boost"], "—")


class WorkflowStateSafetyTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="workflow@example.com",
            email="workflow@example.com",
            password="test-password-123",
        )
        self.job = JobListing.objects.create(
            source="test",
            external_id="job-safety-1",
            title="Principal Engineer",
            company_name="Apex Global",
            description="Lead systems architecture and security.",
        )
        self.profile = SearchProfile.objects.create(
            owner=self.user,
            name="Principal Profile",
            slug="principal",
            is_default=True,
        )

    def test_unsave_does_not_delete_applied_or_applying_jobs(self):
        """Unsave must protect jobs in APPLYING or DONE stages to preserve application history."""
        # Create an applied entry
        applied_entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="principal",
            search_profile=self.profile,
            stage=PipelineEntry.Stage.DONE,
        )

        removed = remove_pipeline_entry_on_unsave(
            self.user,
            job_listing_id=self.job.id,
            slug="principal",
        )
        self.assertEqual(removed, 0)

        applied_entry.refresh_from_db()
        self.assertIsNone(applied_entry.removed_at)
        self.assertEqual(applied_entry.stage, PipelineEntry.Stage.DONE)

    def test_save_is_monotonic_and_does_not_regress_applied_stage(self):
        """Re-saving a previously removed or active applied job does not reset its stage to VETTING."""
        entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="principal",
            search_profile=self.profile,
            stage=PipelineEntry.Stage.APPLYING,
        )

        saved_entry, landed_in_review = ensure_pipeline_entry_on_manual_save(
            self.user,
            job_listing_id=self.job.id,
            slug="principal",
        )
        self.assertEqual(saved_entry.stage, PipelineEntry.Stage.APPLYING)
        self.assertFalse(landed_in_review)


class LegalCompliancePagesTest(TestCase):
    def test_legal_pages_render_and_contain_no_placeholders(self):
        """Privacy Policy and Terms of Service must render without placeholder copy."""
        for path in ("/legal/privacy/", "/legal/terms/"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            content = resp.content.decode("utf-8")
            self.assertNotIn("Replace this placeholder", content)
            self.assertNotIn("use the support channel for your deployment", content)
            self.assertIn("October 22, 2026", content)
            self.assertIn("privacy@hireedge.com", content) if "privacy" in path else self.assertIn("legal@hireedge.com", content)


class SearchProfileDualReadTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dualread@example.com",
            email="dualread@example.com",
            password="test-password-123",
        )
        self.profile = SearchProfile.objects.create(
            owner=self.user,
            name="Cloud Architect",
            slug="cloud-architect",
            is_default=True,
        )
        self.job1 = JobListing.objects.create(
            source="test",
            external_id="job-dual-1",
            title="Cloud Architect",
            company_name="Acme Cloud",
            description="AWS GCP Kubernetes architecture.",
        )
        self.job2 = JobListing.objects.create(
            source="test",
            external_id="job-dual-2",
            title="Senior DevOps",
            company_name="Beta Ops",
            description="Terraform CI/CD pipelines.",
        )

    def test_dual_read_queries_match_both_search_profile_fk_and_legacy_track_slug(self):
        from resume_app.models import JobListingAction
        from resume_app.track_actions import (
            q_saved_rows_for_track,
            q_disliked_rows_for_search,
            q_preference_embedding_track,
        )

        # Action 1: Linked to SearchProfile FK
        JobListingAction.objects.create(
            owner=self.user,
            job_listing=self.job1,
            action=JobListingAction.ActionType.SAVED,
            search_profile=self.profile,
            track=self.profile.slug,
        )

        # Action 2: Legacy row with only track slug
        JobListingAction.objects.create(
            owner=self.user,
            job_listing=self.job2,
            action=JobListingAction.ActionType.DISLIKED,
            search_profile=None,
            track=self.profile.slug,
        )

        # Query using SearchProfile slug + user
        saved_q = q_saved_rows_for_track(self.profile.slug, user=self.user)
        saved_jobs = list(JobListingAction.objects.filter(owner=self.user).filter(saved_q).values_list("job_listing_id", flat=True))
        self.assertIn(self.job1.id, saved_jobs)

        disliked_q = q_disliked_rows_for_search(self.profile.slug, user=self.user)
        disliked_jobs = list(JobListingAction.objects.filter(owner=self.user).filter(disliked_q).values_list("job_listing_id", flat=True))
        self.assertIn(self.job2.id, disliked_jobs)

        embed_q = q_preference_embedding_track(self.profile.slug, user=self.user)
        total_actions = JobListingAction.objects.filter(owner=self.user).filter(embed_q).count()
        self.assertEqual(total_actions, 2)


class OpenTelemetryToggleTest(TestCase):
    @override_settings(OTEL_ENABLED=False)
    def test_otel_disabled_by_default_returns_false_safely(self):
        from resume_app.otel import init_opentelemetry, get_current_trace_context

        # Initialization returns False with zero overhead
        result = init_opentelemetry()
        self.assertFalse(result)

        # Trace context returns safe empty dictionary
        ctx = get_current_trace_context()
        self.assertIsInstance(ctx, dict)


