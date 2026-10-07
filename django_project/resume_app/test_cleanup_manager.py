"""Tests for Cleanup Manager: cross-profile deduplication and 2-week retention purge."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from resume_app.job_dedupe import dedupe_pipeline_entries
from resume_app.models import (
    AppAutomationSettings,
    JobListing,
    JobListingTrackMetrics,
    PipelineEntry,
    Track,
)
from resume_app.tasks import apply_cleanup_retention_purge, apply_pipeline_cleanup_policies

User = get_user_model()


class CleanupManagerDedupeAndRetentionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cleanup_user", password="password123!")
        self.other_user = User.objects.create_user(username="other_cleanup_user", password="password123!")
        Track.ensure_baseline(self.user)
        Track.ensure_baseline(self.other_user)
        self.cfg = AppAutomationSettings.get_for_user(self.user)

    def test_cross_profile_dedupe_keeps_highest_fit_match_entry(self):
        """Cross-profile dedupe should keep the entry in the profile with highest fit/match %."""
        job = JobListing.objects.create(
            title="Senior Staff Software Engineer",
            company_name="CloudScale Corp",
            description="Build scalable distributed backend systems with Python and Kubernetes.",
            source="indeed",
        )

        # Profile 1 (track: 'ic') - Focus/Fit 65%
        entry_ic = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            focus_percent=65,
            focus_after_penalty=65,
            preference_margin=1,
        )

        # Profile 2 (track: 'mgmt') - Focus/Fit 92% (Winner)
        entry_mgmt = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="mgmt",
            stage=PipelineEntry.Stage.PIPELINE,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="mgmt",
            focus_percent=92,
            focus_after_penalty=92,
            preference_margin=4,
        )

        res = dedupe_pipeline_entries(user=self.user, track_slug="*", stage="all", include_done=False)
        self.assertEqual(res["entries_removed"], 1)

        entry_ic.refresh_from_db()
        entry_mgmt.refresh_from_db()

        # Highest fit % (mgmt, 92%) must remain active
        self.assertIsNone(entry_mgmt.removed_at)
        self.assertEqual(entry_mgmt.stage, PipelineEntry.Stage.PIPELINE)

        # Lower fit % (ic, 65%) must be marked deleted
        self.assertIsNotNone(entry_ic.removed_at)
        self.assertEqual(entry_ic.stage, PipelineEntry.Stage.DELETED)

    def test_dedupe_excludes_applied_done_stage(self):
        """Applied (Done) entries must never be deleted by deduplication."""
        job = JobListing.objects.create(
            title="Lead DevOps Architect",
            company_name="InfraCore",
            description="Oversee cloud automation and infrastructure as code across AWS.",
            source="linkedin",
        )

        entry_done = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.DONE,
        )
        entry_pipeline = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="mgmt",
            stage=PipelineEntry.Stage.PIPELINE,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job,
            track="mgmt",
            focus_percent=99,
            focus_after_penalty=99,
        )

        res = dedupe_pipeline_entries(user=self.user, track_slug="*", stage="all", include_done=False)
        entry_done.refresh_from_db()
        entry_pipeline.refresh_from_db()

        # Applied / Done entry is protected and not removed
        self.assertIsNone(entry_done.removed_at)
        self.assertEqual(entry_done.stage, PipelineEntry.Stage.DONE)

    def test_tenant_retention_purge_removes_jobs_older_than_two_weeks_by_default(self):
        """Cleanup retention purge should remove non-applied jobs older than 14 days by default."""
        job_old = JobListing.objects.create(
            title="Old Stale Listing",
            company_name="Legacy Tech",
            description="Ancient job from 20 days ago.",
            source="test_old",
            external_id="job_old_1",
        )
        job_recent = JobListing.objects.create(
            title="Fresh Recent Listing",
            company_name="Modern Tech",
            description="Recent job from 3 days ago.",
            source="test_recent",
            external_id="job_recent_1",
        )

        old_entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_old,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
        )
        # Backdate fetched_at to 20 days ago (Sourced Date)
        JobListing.objects.filter(id=job_old.id).update(
            fetched_at=timezone.now() - timedelta(days=20)
        )

        recent_entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_recent,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
        )
        # Backdate fetched_at to 3 days ago
        JobListing.objects.filter(id=job_recent.id).update(
            fetched_at=timezone.now() - timedelta(days=3)
        )

        removed_count = apply_cleanup_retention_purge(self.cfg)
        self.assertEqual(removed_count, 1)

        # Old entry was removed / expired from pipeline
        old_entry.refresh_from_db()
        self.assertEqual(old_entry.stage, PipelineEntry.Stage.EXPIRED)
        self.assertIsNotNone(old_entry.removed_at)
        # Recent entry is preserved
        recent_entry.refresh_from_db()
        self.assertEqual(recent_entry.stage, PipelineEntry.Stage.PIPELINE)
        self.assertIsNone(recent_entry.removed_at)

    def test_tenant_retention_purge_configurable_per_tenant(self):
        """Tenants can configure custom retention days (e.g. 30 days)."""
        self.cfg.cleanup_job_retention_days = 30
        self.cfg.cleanup_pipeline_retention_days = 30
        self.cfg.cleanup_vetting_retention_days = 30
        self.cfg.cleanup_applying_retention_days = 30
        self.cfg.save()

        job = JobListing.objects.create(
            title="Twenty Day Old Role",
            company_name="Mid-Term Tech",
            description="Job from 20 days ago.",
            source="test_custom",
            external_id="job_custom_1",
        )
        JobListing.objects.filter(id=job.id).update(
            fetched_at=timezone.now() - timedelta(days=20)
        )
        entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
        )

        # With 30 day retention, the 20-day-old job should NOT be removed
        removed_count = apply_cleanup_retention_purge(self.cfg)
        self.assertEqual(removed_count, 0)
        entry.refresh_from_db()
        self.assertEqual(entry.stage, PipelineEntry.Stage.PIPELINE)
        self.assertIsNone(entry.removed_at)

    def test_cross_board_deduplication_real_world_example(self):
        """
        Verify that postings across LinkedIn, Adzuna, and Indeed for the same company/role
        (e.g., JPMorganChase vs JPMorgan Chase Bank, N.A. with varying description headers)
        correctly collapse to 1 winner, while distinct companies (Capital One) are preserved.
        """
        jpm_desc = (
            "Your opportunity to make a real impact and shape the future of financial services is waiting for you. "
            "Let's push the boundaries of what's possible together. As a Senior Director of Software Engineering at "
            "JPMorganChase within the Corporate Technology line of business, you will own the technical vision."
        )

        # 1. LinkedIn: "JPMorganChase ·Plano, TX", "**Job Description** Your opportunity..."
        job_linkedin = JobListing.objects.create(
            title="Senior Director of Software Engineering - CCB Risk Technology Feature Platform Engineering",
            company_name="JPMorganChase ·Plano, TX",
            description=f"**Job Description** {jpm_desc}",
            source="linkedin",
            external_id="jpm_li_1",
        )
        entry_li = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_linkedin,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
            vetting_interview_probability=38,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job_linkedin,
            track="ic",
            focus_percent=86,
            focus_after_penalty=86,
        )

        # 2. Adzuna: "JPMorgan Chase Bank, N.A. ·Plano, Collin County", raw description without header
        job_adzuna = JobListing.objects.create(
            title="Senior Director of Software Engineering - CCB Risk Technology Feature Platform Engineering",
            company_name="JPMorgan Chase Bank, N.A. ·Plano, Collin County",
            description=jpm_desc,
            source="adzuna",
            external_id="jpm_adz_1",
        )
        entry_adz = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_adzuna,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job_adzuna,
            track="ic",
            focus_percent=81,
            focus_after_penalty=81,
        )

        # 3. Indeed: "JPMorganChase ·Plano, TX, US", "**JOB DESCRIPTION** Your opportunity..."
        job_indeed = JobListing.objects.create(
            title="Senior Director of Software Engineering - CCB Risk Technology Feature Platform Engineering",
            company_name="JPMorganChase ·Plano, TX, US",
            description=f"**JOB DESCRIPTION** {jpm_desc}",
            source="indeed",
            external_id="jpm_ind_1",
        )
        entry_ind = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_indeed,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
            vetting_interview_probability=42,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job_indeed,
            track="ic",
            focus_percent=86,
            focus_after_penalty=86,
        )

        # 4. Capital One: Distinct company and title
        job_capone = JobListing.objects.create(
            title="Senior Director, Software Engineering (Developer Experience)",
            company_name="Capital One ·Plano, TX",
            description="As a Capital One Senior Director of Software Engineering, you'll work on everything...",
            source="linkedin",
            external_id="capone_li_1",
        )
        entry_capone = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_capone,
            track="ic",
            stage=PipelineEntry.Stage.PIPELINE,
            vetting_interview_probability=68,
        )
        JobListingTrackMetrics.objects.create(
            owner=self.user,
            job_listing=job_capone,
            track="ic",
            focus_percent=84,
            focus_after_penalty=84,
        )

        res = dedupe_pipeline_entries(user=self.user, track_slug="*", stage="all", include_done=False)
        self.assertEqual(res["duplicate_groups"], 1)
        self.assertEqual(res["entries_removed"], 2)

        entry_li.refresh_from_db()
        entry_adz.refresh_from_db()
        entry_ind.refresh_from_db()
        entry_capone.refresh_from_db()

        # The 3 JPMorganChase postings are deduplicated into 1 winner (Indeed with 86% match and 42% interview probability)
        self.assertEqual(entry_ind.stage, PipelineEntry.Stage.PIPELINE)
        self.assertIsNone(entry_ind.removed_at)

        # The other 2 JPMorganChase copies are removed
        self.assertEqual(entry_li.stage, PipelineEntry.Stage.DELETED)
        self.assertEqual(entry_adz.stage, PipelineEntry.Stage.DELETED)

        # Capital One posting remains active and untouched
        self.assertEqual(entry_capone.stage, PipelineEntry.Stage.PIPELINE)
        self.assertIsNone(entry_capone.removed_at)

    def test_pipeline_cleanup_policies_applies_thresholds_and_preserves_applied_stage(self):
        """Cleanup applies purge thresholds & retention to pre-applied stages, strictly preserving APPLIED (Done) stage."""
        self.cfg.pipeline_purge_match_score_max = 25
        self.cfg.cleanup_pipeline_retention_days = 7
        self.cfg.save()

        now = timezone.now()

        # 1. Low scoring role in Review (15% < 25%) -> should be purged
        job_low_review = JobListing.objects.create(title="Low Score Review Role", source="dice", external_id="dice-clean-1")
        entry_low_review = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_low_review,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
            vetting_interview_probability=15,
        )

        # 2. Low scoring role in Applying (18% < 25%) -> should be purged
        job_low_applying = JobListing.objects.create(title="Low Score Applying Role", source="dice", external_id="dice-clean-2")
        entry_low_applying = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_low_applying,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
            vetting_interview_probability=18,
        )

        # 3. Low scoring role in APPLIED (Done) stage (10% < 25%) -> MUST BE PRESERVED
        job_done = JobListing.objects.create(title="Applied Low Score Role", source="dice", external_id="dice-clean-3")
        entry_done = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_done,
            track="ic",
            stage=PipelineEntry.Stage.DONE,
            vetting_interview_probability=10,
        )

        # 4. Old role in Review older than retention (10 days > 7) -> should be purged
        job_old = JobListing.objects.create(title="Old Role", source="dice", external_id="dice-clean-4")
        JobListing.objects.filter(id=job_old.id).update(fetched_at=now - timedelta(days=10))
        entry_old = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_old,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
            vetting_interview_probability=50,
        )

        # 5. Good fresh role in Review (75% >= 25%, fresh) -> MUST BE PRESERVED
        job_good = JobListing.objects.create(title="Good Fresh Role", source="dice", external_id="dice-clean-5")
        entry_good = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=job_good,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
            vetting_interview_probability=75,
        )

        res = apply_pipeline_cleanup_policies(self.user)
        self.assertGreaterEqual(res["purged"], 3)

        # Verify entry states
        entry_low_review.refresh_from_db()
        self.assertEqual(entry_low_review.stage, PipelineEntry.Stage.EXPIRED)
        self.assertIsNotNone(entry_low_review.removed_at)

        entry_low_applying.refresh_from_db()
        self.assertEqual(entry_low_applying.stage, PipelineEntry.Stage.EXPIRED)
        self.assertIsNotNone(entry_low_applying.removed_at)

        entry_old.refresh_from_db()
        self.assertEqual(entry_old.stage, PipelineEntry.Stage.EXPIRED)
        self.assertIsNotNone(entry_old.removed_at)

        # APPLIED (DONE) stage is strictly preserved despite 10% score
        entry_done.refresh_from_db()
        self.assertEqual(entry_done.stage, PipelineEntry.Stage.DONE)
        self.assertIsNone(entry_done.removed_at)

        # Good fresh role is preserved
        entry_good.refresh_from_db()
        self.assertEqual(entry_good.stage, PipelineEntry.Stage.VETTING)
        self.assertIsNone(entry_good.removed_at)

