"""Tests for the Autonomous Apply Agent: detection, adapters, resolver, orchestrator."""
from __future__ import annotations

import contextlib
import time
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from .apply_agent import ats_detect, orchestrator
from .apply_agent import generic_agent
from .apply_agent import resolve_and_detect as resolver
from .apply_agent.adapters import get_adapter, supported_ats
from .apply_agent.base import ApplyContext, FillResult, SubmitResult, standard_answer_key
from .models import (
    AppAutomationSettings,
    ApplicantProfile,
    ApplicationAttempt,
    AtsAutoSubmitStats,
    JobDescription,
    JobListing,
    LLMProviderConfig,
    OptimizedResume,
    PipelineEntry,
    UserResume,
)


def _ctx(**overrides) -> ApplyContext:
    base = dict(
        full_name="Jane Q Doe",
        email="jane@example.com",
        phone="555-1234",
        location="Austin, TX",
        linkedin_url="https://linkedin.com/in/jane",
        website_url="",
        work_authorization="US Citizen",
        requires_sponsorship=False,
        salary_expectation="",
        cover_letter="Hi {company}, re {title}.",
        custom_qa={"GitHub URL": "https://github.com/jane"},
        include_eeo=False,
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        resume_file_path="/tmp/resume.pdf",
        company_name="Acme",
        job_title="Engineer",
    )
    base.update(overrides)
    return ApplyContext(**base)


class AtsDetectTests(TestCase):
    def test_detect_known_hosts(self):
        self.assertEqual(ats_detect.detect_ats_from_url("https://boards.greenhouse.io/acme/jobs/1"), "greenhouse")
        self.assertEqual(ats_detect.detect_ats_from_url("https://jobs.lever.co/acme/abc"), "lever")
        self.assertEqual(ats_detect.detect_ats_from_url("https://jobs.ashbyhq.com/acme"), "ashby")
        self.assertEqual(ats_detect.detect_ats_from_url("https://acme.wd1.myworkdayjobs.com/x"), "workday")

    def test_detect_unknown(self):
        self.assertEqual(ats_detect.detect_ats_from_url("https://careers.acme.com/job/1"), ats_detect.ATS_UNKNOWN)
        self.assertEqual(ats_detect.detect_ats_from_url(""), ats_detect.ATS_UNKNOWN)


class AdapterTests(TestCase):
    def test_registry_excludes_workday(self):
        self.assertIsNotNone(get_adapter("greenhouse"))
        self.assertIsNotNone(get_adapter("lever"))
        self.assertIsNone(get_adapter("workday"))
        self.assertIn("greenhouse", supported_ats())

    def test_greenhouse_answer_key_has_split_name(self):
        adapter = get_adapter("greenhouse")
        key = adapter.build_answer_key(_ctx())
        self.assertEqual(key["first_name"], "Jane")
        self.assertEqual(key["last_name"], "Doe")
        self.assertEqual(key["email"], "jane@example.com")

    def test_lever_answer_key_uses_full_name(self):
        adapter = get_adapter("lever")
        key = adapter.build_answer_key(_ctx())
        self.assertEqual(key["full_name"], "Jane Q Doe")

    def test_standard_answer_key_drops_empty_and_merges_custom(self):
        key = standard_answer_key(_ctx(website_url=""))
        self.assertNotIn("website", key)
        self.assertEqual(key["GitHub URL"], "https://github.com/jane")
        # Cover letter template is rendered with company/title.
        self.assertIn("Acme", key["cover_letter"])

    def test_can_handle(self):
        self.assertTrue(get_adapter("greenhouse").can_handle("https://boards.greenhouse.io/x"))
        self.assertFalse(get_adapter("greenhouse").can_handle("https://jobs.lever.co/x"))


class ResolverTests(TestCase):
    def test_mock_known_host_direct(self):
        result = resolver.resolve_and_detect("https://boards.greenhouse.io/acme/jobs/1", use_mock=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.ats_type, "greenhouse")

    def test_mock_unmapped_aggregator_is_unresolved(self):
        result = resolver.resolve_and_detect("https://www.indeed.com/viewjob?jk=zzz", use_mock=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "unresolved_url")

    def test_mock_direct_company_unknown_is_ok(self):
        result = resolver.resolve_and_detect("https://careers.acme.com/job/1", use_mock=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.ats_type, ats_detect.ATS_UNKNOWN)

    @override_settings(APPLY_MOCK_URL_MAP={"https://x.test/j": ["https://jobs.lever.co/acme/1", "lever"]})
    def test_mock_map_override(self):
        result = resolver.resolve_and_detect("https://x.test/j", use_mock=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.ats_type, "lever")
        self.assertEqual(result.apply_url, "https://jobs.lever.co/acme/1")


class _FakeAdapter:
    ats_id = "greenhouse"

    def __init__(self, fill_ok=True, confidence=0.9, submit=None):
        self._fill_ok = fill_ok
        self._confidence = confidence
        self._submit = submit or SubmitResult(ok=True, confirmed=True)

    def can_handle(self, url):
        return True

    def build_answer_key(self, ctx):
        return {"email": ctx.email}

    def fill_application(self, ctx, *, stop_before_submit):
        return FillResult(ok=self._fill_ok, confidence=self._confidence, payload={"email": ctx.email})

    def fill_from_payload(self, ctx, payload):
        return FillResult(ok=self._fill_ok, confidence=self._confidence, payload=payload)

    def submit_and_verify(self, ctx):
        return self._submit


@contextlib.contextmanager
def _fake_browser(**kwargs):
    yield object()


class OrchestratorTests(TestCase):
    def setUp(self):
        self.job = JobListing.objects.create(
            source="test",
            external_id="t1",
            title="Engineer",
            company_name="Acme",
            url="https://boards.greenhouse.io/acme/jobs/1",
        )
        self.entry = PipelineEntry.objects.create(
            job_listing=self.job, track="ic", stage=PipelineEntry.Stage.APPLYING
        )
        ApplicantProfile.objects.create(pk=1, full_name="Jane Doe", email="jane@example.com")

    def _completed_optimization(self):
        ur = UserResume.objects.create(file="r.pdf", is_library=True)
        jd = JobDescription.objects.create(content="JD")
        return OptimizedResume.objects.create(
            original_resume=ur,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="Resume body",
            pipeline_entry=self.entry,
            ats_score=90,
            recruiter_score=88,
        )

    def test_start_creates_attempt_and_skips_duplicates(self):
        created = orchestrator.start_attempts_for_entries([self.entry.id])
        self.assertEqual(len(created), 1)
        # A second start while one is active is a no-op.
        again = orchestrator.start_attempts_for_entries([self.entry.id])
        self.assertEqual(len(again), 0)

    def test_start_skips_non_applying(self):
        self.entry.stage = PipelineEntry.Stage.VETTING
        self.entry.save()
        created = orchestrator.start_attempts_for_entries([self.entry.id])
        self.assertEqual(len(created), 0)

    def test_optimizing_waits_when_no_optimization(self):
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry, status=ApplicationAttempt.Status.OPTIMIZING
        )
        with patch.object(orchestrator, "_enqueue_optimization") as mock_enq:
            orchestrator.advance_attempt(attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.WAITING_OPTIMIZER)
        mock_enq.assert_called_once()

    def test_full_semi_auto_flow_to_succeeded(self):
        self._completed_optimization()
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.OPTIMIZING,
            automation_mode=ApplicationAttempt.Mode.SEMI_AUTO,
        )
        with patch.object(orchestrator, "_export_resume_file", return_value="/tmp/x.pdf"), \
             patch.object(orchestrator, "browser_session_factory", _fake_browser), \
             patch.object(orchestrator, "get_adapter", return_value=_FakeAdapter()):
            # optimizing -> resolve_and_detect (resume attached, exported)
            orchestrator.advance_attempt(attempt.id)
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, ApplicationAttempt.Status.RESOLVE_AND_DETECT)

            # resolve -> dry_run_fill
            orchestrator.advance_attempt(attempt.id)
            attempt.refresh_from_db()
            self.assertEqual(attempt.ats_type, "greenhouse")
            self.assertEqual(attempt.status, ApplicationAttempt.Status.DRY_RUN_FILL)

            # dry run -> awaiting_approval (semi-auto)
            orchestrator.advance_attempt(attempt.id)
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, ApplicationAttempt.Status.AWAITING_APPROVAL)
            self.assertEqual(attempt.fill_payload_json, {"email": "jane@example.com"})

            # approve -> submitting -> succeeded
            orchestrator.approve_attempt(attempt.id)
            orchestrator.advance_attempt(attempt.id)
            attempt.refresh_from_db()

        self.assertEqual(attempt.status, ApplicationAttempt.Status.SUCCEEDED)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.DONE)

    def test_approve_unknown_ats_marks_done_without_submit(self):
        self._completed_optimization()
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.AWAITING_APPROVAL,
            ats_type="unknown",
            apply_url="https://careers.example.com/job/1",
            fill_payload_json={"email": "jane@example.com"},
        )
        orchestrator.approve_attempt(attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.SUCCEEDED)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.DONE)

    def test_submit_ambiguous_marks_failed_without_done(self):
        self._completed_optimization()
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.SUBMITTING,
            ats_type="greenhouse",
            fill_payload_json={"email": "jane@example.com"},
            resume_file_path="/tmp/x.pdf",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        )
        ambiguous = SubmitResult(ok=False, confirmed=False, error_code="submit_ambiguous", message="no confirm")
        with patch.object(orchestrator, "browser_session_factory", _fake_browser), \
             patch.object(orchestrator, "get_adapter", return_value=_FakeAdapter(submit=ambiguous)):
            orchestrator.advance_attempt(attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, "submit_ambiguous")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.stage, PipelineEntry.Stage.APPLYING)

    def test_unresolved_url_fails(self):
        self.job.url = "https://www.indeed.com/viewjob?jk=unmapped"
        self.job.save()
        self._completed_optimization()
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry, status=ApplicationAttempt.Status.RESOLVE_AND_DETECT
        )
        orchestrator.advance_attempt(attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, "unresolved_url")

    def test_override_url_resets_to_dry_run(self):
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.FAILED,
            error_code="unresolved_url",
        )
        orchestrator.set_override_url(attempt.id, "https://jobs.lever.co/acme/1")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.DRY_RUN_FILL)
        self.assertEqual(attempt.ats_type, "lever")

    def test_full_auto_requires_graduated_ats(self):
        self._completed_optimization()
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.DRY_RUN_FILL,
            automation_mode=ApplicationAttempt.Mode.FULL_AUTO,
            ats_type="greenhouse",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        )
        with patch.object(orchestrator, "browser_session_factory", _fake_browser), \
             patch.object(orchestrator, "get_adapter", return_value=_FakeAdapter()):
            # No graduation yet -> still requires review.
            orchestrator.advance_attempt(attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.AWAITING_APPROVAL)

        # Graduate the ATS and retry.
        AtsAutoSubmitStats.objects.create(ats_type="greenhouse", full_auto_enabled=True)
        attempt.status = ApplicationAttempt.Status.DRY_RUN_FILL
        attempt.save()
        with patch.object(orchestrator, "browser_session_factory", _fake_browser), \
             patch.object(orchestrator, "get_adapter", return_value=_FakeAdapter()):
            orchestrator.advance_attempt(attempt.id)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.SUBMITTING)


class FullAutoRampTests(TestCase):
    def test_clean_submit_streak_enables_full_auto(self):
        AppAutomationSettings.objects.create(pk=1, apply_full_auto_min_clean_submits=3)
        for _ in range(2):
            orchestrator._record_clean_submit("greenhouse")
        stats = AtsAutoSubmitStats.objects.get(ats_type="greenhouse")
        self.assertFalse(stats.full_auto_enabled)
        orchestrator._record_clean_submit("greenhouse")
        stats.refresh_from_db()
        self.assertTrue(stats.full_auto_enabled)
        self.assertEqual(stats.clean_submit_streak, 3)

    def test_correction_resets_streak(self):
        AppAutomationSettings.objects.create(pk=1, apply_full_auto_min_clean_submits=3)
        orchestrator._record_clean_submit("greenhouse")
        orchestrator._record_correction("greenhouse")
        stats = AtsAutoSubmitStats.objects.get(ats_type="greenhouse")
        self.assertEqual(stats.clean_submit_streak, 0)
        self.assertFalse(stats.full_auto_enabled)
        self.assertEqual(stats.total_corrections, 1)


class MemoryLeakFixTests(TestCase):
    def setUp(self):
        self.job = JobListing.objects.create(
            source="test",
            external_id="t-mem",
            title="Engineer",
            company_name="Acme",
            url="https://boards.greenhouse.io/acme/jobs/1",
        )
        self.entry = PipelineEntry.objects.create(
            job_listing=self.job, track="ic", stage=PipelineEntry.Stage.APPLYING
        )
        ApplicantProfile.objects.create(pk=1, full_name="Jane Doe", email="jane@example.com")

    def test_generic_fill_skips_playwright_session(self):
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.DRY_RUN_FILL,
            ats_type="unknown",
            apply_url="https://careers.acme.com/job/1",
            resume_file_path="/tmp/x.pdf",
        )
        fill_result = FillResult(ok=True, confidence=0.5, payload={"email": "jane@example.com"})
        browser_factory = MagicMock()
        with patch.object(orchestrator, "browser_session_factory", browser_factory), \
             patch.object(orchestrator, "get_adapter", return_value=None), \
             patch("resume_app.apply_agent.generic_agent.run_generic_fill", return_value=fill_result):
            orchestrator.advance_attempt(attempt.id)
        browser_factory.assert_not_called()

    @override_settings(APPLY_USE_MOCK_RESOLVER=False, APPLY_BROWSER_STEP_TIMEOUT_SECONDS=30)
    def test_resolve_live_acquires_browser_slot(self):
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.RESOLVE_AND_DETECT,
            resume_file_path="/tmp/x.pdf",
        )
        ok_result = resolver.ResolveResult(ok=True, apply_url=self.job.url, ats_type="greenhouse")
        with patch.object(orchestrator, "_acquire_browser_slot", return_value=True) as acquire, \
             patch.object(orchestrator, "_release_browser_slot") as release, \
             patch.object(resolver, "resolve_and_detect", return_value=ok_result):
            orchestrator.advance_attempt(attempt.id)
        acquire.assert_called_once()
        release.assert_called_once()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.DRY_RUN_FILL)

    @override_settings(APPLY_BROWSER_STEP_TIMEOUT_SECONDS=1)
    def test_run_browser_step_wall_clock_timeout(self):
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            status=ApplicationAttempt.Status.DRY_RUN_FILL,
            ats_type="greenhouse",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
            resume_file_path="/tmp/x.pdf",
        )

        def _hang():
            time.sleep(5)
            return "never"

        with patch("resume_app.apply_agent.browser.kill_orphan_chromium") as kill_orphans:
            outcome = orchestrator._run_browser_step(_hang, attempt, default_error="fill_failed")
        self.assertIsNone(outcome)
        kill_orphans.assert_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ApplicationAttempt.Status.FAILED)
        self.assertEqual(attempt.error_code, ApplicationAttempt.ERROR_AUTOMATION_TIMEOUT)

    def test_get_redis_singleton(self):
        import resume_app.llm_rate_limit as rl

        rl._redis_client = None
        mock_client = MagicMock()
        with patch("redis.Redis", return_value=mock_client) as redis_ctor:
            first = rl._get_redis()
            second = rl._get_redis()
        self.assertIs(first, second)
        redis_ctor.assert_called_once()
        rl._redis_client = None


class ApplyAgentLlmSettingsTests(TestCase):
    def setUp(self):
        from .crypto import encrypt_api_key

        self.cfg = LLMProviderConfig.objects.create(
            provider="Groq",
            encrypted_api_key=encrypt_api_key("test-key"),
            default_model="openai/gpt-oss-120b",
            is_active=True,
        )
        self.settings_solo = AppAutomationSettings.get_solo()

    def test_dedicated_provider_overrides_global(self):
        self.settings_solo.apply_agent_llm_provider = "Groq"
        self.settings_solo.apply_agent_llm_model = "openai/gpt-oss-120b"
        self.settings_solo.save()
        cand = generic_agent.resolve_apply_agent_llm_candidate()
        self.assertEqual(cand["provider"], "Groq")
        self.assertEqual(cand["model"], "openai/gpt-oss-120b")

    def test_blank_provider_falls_back_to_runtime(self):
        self.settings_solo.apply_agent_llm_provider = ""
        self.settings_solo.apply_agent_llm_model = ""
        self.settings_solo.save()
        cand = generic_agent.resolve_apply_agent_llm_candidate()
        self.assertEqual(cand["provider"], "Groq")
        self.assertTrue(cand["model"])

    def test_missing_dedicated_provider_raises(self):
        self.settings_solo.apply_agent_llm_provider = "Anthropic"
        self.settings_solo.save()
        with self.assertRaises(ValueError):
            generic_agent.resolve_apply_agent_llm_candidate()


class StepCaptureTests(TestCase):
    def test_save_and_media_url(self):
        import tempfile

        from .apply_agent.step_capture import media_url_for_path, save_step_screenshot

        with tempfile.TemporaryDirectory() as tmp:
            with self.settings(MEDIA_ROOT=tmp, MEDIA_URL="/media/"):
                path = save_step_screenshot(99, "step_001", b"\x89PNG\r\n")
                self.assertTrue(path.endswith("step_001.png"))
                url = media_url_for_path(path)
                self.assertIn("/media/apply_agent/attempt_99/step_001.png", url.replace("\\", "/"))


class ApplyBrowserHeadlessTests(TestCase):
    @override_settings(APPLY_BROWSER_HEADLESS=True)
    def test_env_headless_true_by_default(self):
        from .apply_agent.browser import apply_browser_headless

        AppAutomationSettings.get_solo().apply_browser_show_window = False
        AppAutomationSettings.get_solo().save(update_fields=["apply_browser_show_window"])
        self.assertTrue(apply_browser_headless())

    @override_settings(APPLY_BROWSER_HEADLESS=False)
    def test_env_headless_false_shows_window(self):
        from .apply_agent.browser import apply_browser_headless

        self.assertFalse(apply_browser_headless())

    @override_settings(APPLY_BROWSER_HEADLESS=True)
    def test_profile_show_window_overrides_headless(self):
        from .apply_agent.browser import apply_browser_headless

        solo = AppAutomationSettings.get_solo()
        solo.apply_browser_show_window = True
        solo.save(update_fields=["apply_browser_show_window"])
        self.assertFalse(apply_browser_headless())
