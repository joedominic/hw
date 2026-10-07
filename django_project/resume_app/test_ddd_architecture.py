"""
Unit and integration tests for Domain-Driven Design (DDD) components:
- Domain Value Objects (MatchScore, CronSchedule, SalaryRange, TokenUsage, TrackSlug)
- Domain Events and DomainEventBus
- Sourcing Anti-Corruption Layer (RawJobDTO, JobIngestionService)
- Application Services (PipelineApplicationService, SearchApplicationService)
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .application.pipeline_services import PipelineApplicationService
from .domain.event_bus import DomainEventBus
from .domain.events import JobPromotedToApplying, JobPromotedToVetting
from .domain.value_objects import (
    CronSchedule,
    MatchScore,
    SalaryRange,
    TokenUsage,
    TrackSlug,
)
from .models import JobListing, PipelineEntry, SearchProfile, Track
from .sourcing.ports import JobSourcePort, RawJobDTO
from .sourcing.service import JobIngestionService

User = get_user_model()


class ValueObjectsTestCase(TestCase):
    def test_match_score_invariants(self):
        score = MatchScore(85)
        self.assertEqual(int(score), 85)
        self.assertEqual(score.grade, "Strong Match")
        self.assertEqual(str(score), "85%")

        # Test penalty
        penalized = score.apply_penalty(20)
        self.assertEqual(int(penalized), 65)
        self.assertEqual(penalized.grade, "Low Match")

        # Invalid bounds raise ValueError
        with self.assertRaises(ValueError):
            MatchScore(105)
        with self.assertRaises(ValueError):
            MatchScore(-5)

        # Comparisons
        self.assertTrue(MatchScore(90) > MatchScore(80))
        self.assertTrue(MatchScore(70) <= MatchScore(70))

    def test_cron_schedule_invariants(self):
        from datetime import timezone as dt_timezone
        schedule = CronSchedule("0 9 * * 1-5")
        self.assertEqual(str(schedule), "0 9 * * 1-5")
        base = datetime(2026, 8, 28, 8, 0, tzinfo=dt_timezone.utc)
        next_dt = schedule.next_run_after(base)
        self.assertEqual(next_dt.hour, 9)
        self.assertEqual(next_dt.minute, 0)

        with self.assertRaises(ValueError):
            CronSchedule("invalid cron")

    def test_salary_range(self):
        salary = SalaryRange(min_amount=Decimal(120000), max_amount=Decimal(150000))
        self.assertEqual(salary.format_display(), "$120,000 - $150,000 / yearly")

        empty = SalaryRange()
        self.assertEqual(empty.format_display(), "Compensation not specified")

    def test_token_usage_arithmetic(self):
        usage1 = TokenUsage(input_tokens=100, output_tokens=50)
        usage2 = TokenUsage(input_tokens=200, output_tokens=100)
        combined = usage1 + usage2
        self.assertEqual(combined.input_tokens, 300)
        self.assertEqual(combined.output_tokens, 150)
        self.assertEqual(combined.total_tokens, 450)
        self.assertTrue(combined.is_within_budget(500))
        self.assertFalse(combined.is_within_budget(400))

    def test_track_slug_validation(self):
        slug = TrackSlug("engineering-ic")
        self.assertEqual(str(slug), "engineering-ic")

        with self.assertRaises(ValueError):
            TrackSlug("Invalid Slug With Spaces!")
        with self.assertRaises(ValueError):
            TrackSlug("a" * 35)


class DomainEventBusTestCase(TestCase):
    def test_event_publish_and_subscribe(self):
        bus = DomainEventBus()
        received: list[JobPromotedToVetting] = []

        def handler(event: JobPromotedToVetting):
            received.append(event)

        bus.subscribe(JobPromotedToVetting, handler)

        event = JobPromotedToVetting(user_id=1, entry_id=42, track="ic")
        bus.publish(event)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].entry_id, 42)
        self.assertEqual(received[0].track, "ic")

        # Unsubscribe
        bus.unsubscribe(JobPromotedToVetting, handler)
        bus.publish(event)
        self.assertEqual(len(received), 1)


class SourcingACLTestCase(TestCase):
    class MockAdapter(JobSourcePort):
        @property
        def source_name(self) -> str:
            return "mock_board"

        def fetch_jobs(self, search_term: str, location: str | None = None, limit: int = 20, **kwargs):
            return [
                RawJobDTO.create(
                    source="mock_board",
                    title="Staff Engineer",
                    company_name="Acme Inc",
                    url="https://example.com/job/1",
                    location="Remote",
                    description="Lead backend development",
                    posted_at=timezone.now(),
                )
            ]

    def test_job_ingestion_service(self):
        service = JobIngestionService(adapters={"mock": self.MockAdapter()})
        dtos = service.fetch_all("engineer", site_names=["mock"])
        self.assertEqual(len(dtos), 1)
        self.assertEqual(dtos[0].title, "Staff Engineer")
        self.assertEqual(dtos[0].company_name, "Acme Inc")

        # Persist DTOs into domain JobListing entities
        saved = service.persist_dtos(dtos)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].title, "Staff Engineer")
        self.assertEqual(saved[0].source, "mock_board")
        self.assertEqual(JobListing.objects.filter(source="mock_board").count(), 1)


class PipelineApplicationServiceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="app_service_user", password="password")
        Track.ensure_baseline(self.user)
        self.job = JobListing.objects.create(
            source="test_src",
            external_id="ext-99",
            title="Backend Lead",
            company_name="TechCorp",
        )
        self.entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="general",
            stage=PipelineEntry.Stage.PIPELINE,
        )

    def test_promote_pipeline_lifecycle(self):
        # 1. Promote to Vetting
        res_vet = PipelineApplicationService.promote_to_vetting(self.user, [self.entry.id])
        self.assertTrue(res_vet.success)
        self.assertEqual(res_vet.promoted_count, 1)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.VETTING)

        # 2. Promote to Applying
        res_app = PipelineApplicationService.promote_to_applying(self.user, [self.entry.id])
        self.assertTrue(res_app.success)
        self.assertEqual(res_app.promoted_count, 1)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.APPLYING)

        # 3. Mark as Applied (Done)
        res_done = PipelineApplicationService.mark_applied(self.user, [self.entry.id])
        self.assertTrue(res_done.success)
        self.assertEqual(res_done.promoted_count, 1)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.DONE)
        from .models import JobListingAction
        self.assertTrue(
            JobListingAction.objects.filter(
                owner=self.user,
                job_listing=self.job,
                action=JobListingAction.ActionType.LIKED,
            ).exists()
        )

    def test_mark_done_auto_tags_liked_and_clears_opposing_sentiment(self):
        from .models import JobListingAction
        # Simulate an existing DISLIKED sentiment beforehand
        JobListingAction.objects.create(
            owner=self.user,
            job_listing=self.job,
            action=JobListingAction.ActionType.DISLIKED,
            track="general",
        )
        self.assertTrue(
            JobListingAction.objects.filter(
                owner=self.user, job_listing=self.job, action=JobListingAction.ActionType.DISLIKED
            ).exists()
        )

        # Mark done directly
        self.entry.mark_done(save=True)
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.DONE)

        # Must now be LIKED, and DISLIKED must be cleared
        self.assertTrue(
            JobListingAction.objects.filter(
                owner=self.user, job_listing=self.job, action=JobListingAction.ActionType.LIKED
            ).exists()
        )
        self.assertFalse(
            JobListingAction.objects.filter(
                owner=self.user, job_listing=self.job, action=JobListingAction.ActionType.DISLIKED
            ).exists()
        )



class BoundedContextPackageImportTestCase(TestCase):
    def test_all_bounded_contexts_importable(self):
        # Verify domain context
        from resume_app.domain import CronSchedule, MatchScore, TokenUsage, event_bus
        self.assertIsNotNone(MatchScore)
        self.assertIsNotNone(event_bus)

        # Verify sourcing context
        from resume_app.sourcing import JobIngestionService, JobSourcePort, RawJobDTO
        self.assertIsNotNone(JobIngestionService)

        # Verify pipeline context
        from resume_app.pipeline import pipeline_board_view, STAGE_TAB_LABELS_NORMAL
        self.assertIsNotNone(pipeline_board_view)

        # Verify optimizer context
        from resume_app.optimizer import OptimizerApplicationService, create_workflow
        self.assertIsNotNone(OptimizerApplicationService)
        self.assertIsNotNone(create_workflow)

        # Verify llm context
        from resume_app.llm import (
            DEFAULT_MODELS,
            LLM_PROVIDERS,
            get_active_llm_provider,
            get_llm,
            invoke_llm_messages,
            local_llm_available,
        )
        self.assertIsNotNone(invoke_llm_messages)
        self.assertIsNotNone(get_llm)
        self.assertIsNotNone(get_active_llm_provider)

        # Verify subscriptions context
        from resume_app.subscriptions import (
            assert_upload_allowed,
            check_quota,
            consume_quota,
            get_user_plan,
            stripe_enabled,
        )
        self.assertIsNotNone(check_quota)
        self.assertIsNotNone(consume_quota)
        self.assertIsNotNone(assert_upload_allowed)
        self.assertIsNotNone(get_user_plan)

        # Verify application context
        from resume_app.application import PipelineApplicationService, SearchApplicationService
        self.assertIsNotNone(PipelineApplicationService)
        self.assertIsNotNone(SearchApplicationService)

