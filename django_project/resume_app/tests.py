from django.test import TestCase, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch, MagicMock
from .models import (
    AppAutomationSettings,
    JobListingTrackMetrics,
    UserResume,
    JobDescription,
    OptimizedResume,
    JobListing,
    PipelineEntry,
)
from .tasks import (
    apply_pipeline_auto_promotions,
    apply_vetting_to_applying_promotions,
    _enqueue_single_pipeline_resume_optimization,
)
from .services import parse_pdf, PDFParseError
from .llm_services import LLM_PROVIDERS
from .job_sources import _row_to_dict
import os

class ModelsTestCase(TestCase):
    def test_model_creation(self):
        resume = UserResume.objects.create(file="test.pdf")
        jd = JobDescription.objects.create(content="Test JD")
        optimized = OptimizedResume.objects.create(original_resume=resume, job_description=jd)
        self.assertEqual(optimized.status, OptimizedResume.STATUS_QUEUED)

    def test_pipelineentry_stage_helpers(self):
        job = JobListing.objects.create(
            source="test",
            external_id="1",
            title="T",
            company_name="C",
        )
        pe = PipelineEntry.objects.create(job_listing=job, track="ic")
        # Default stage is blank / pipeline
        self.assertEqual(pe.stage, "")
        pe.move_to_vetting()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        pe.move_to_applying()
        self.assertEqual(pe.stage, PipelineEntry.Stage.APPLYING)
        pe.mark_done()
        self.assertEqual(pe.stage, PipelineEntry.Stage.DONE)

class ServiceTestCase(TestCase):
    def test_parse_pdf_placeholder(self):
        self.assertTrue(callable(parse_pdf))

    def test_parse_pdf_raises_on_missing_file(self):
        with self.assertRaises(PDFParseError) as ctx:
            parse_pdf("/nonexistent/path.pdf")
        self.assertIn("not found", str(ctx.exception).lower())

    def test_parse_pdf_raises_on_empty_path(self):
        with self.assertRaises(PDFParseError):
            parse_pdf("")


class JobSourceNormalizationTestCase(TestCase):
    def test_row_to_dict_falls_back_to_linkedin_description_keys(self):
        row = {
            "title": "Staff Engineer",
            "company": "ExampleCo",
            "location": "Remote",
            "job_url": "https://linkedin.com/jobs/view/123",
            "job_description": "LinkedIn full job description body",
        }
        normalized = _row_to_dict(row, "linkedin")
        self.assertEqual(normalized["description"], "LinkedIn full job description body")

    def test_row_to_dict_uses_summary_when_description_missing(self):
        row = {
            "title": "Staff Engineer",
            "company": "ExampleCo",
            "location": "Remote",
            "job_url": "https://linkedin.com/jobs/view/123",
            "summary": "Short LinkedIn summary fallback",
        }
        normalized = _row_to_dict(row, "linkedin")
        self.assertEqual(normalized["description"], "Short LinkedIn summary fallback")


class APITestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def test_status_404_for_invalid_resume_id(self):
        response = self.client.get("/api/resume/status/99999/")
        self.assertEqual(response.status_code, 404)

    @patch("resume_app.api.optimize_resume_task")
    def test_optimize_accepts_valid_request(self, mock_task):
        mock_task.return_value = MagicMock(id="huey-task-id")
        pdf_content = b"%PDF-1.4 fake pdf content"
        uploaded = SimpleUploadedFile(
            "resume.pdf", pdf_content, content_type="application/pdf"
        )
        response = self.client.post(
            "/api/resume/optimize",
            data={
                "job_description": "Test job",
                "llm_provider": "OpenAI",
                "api_key": "test-key",
                "file": uploaded,
            },
        )
        self.assertEqual(
            response.status_code,
            200,
            msg=f"Expected 200, got {response.status_code}: {getattr(response, 'content', b'')[:500]}",
        )
        data = response.json()
        self.assertIn("resume_id", data)
        mock_task.assert_called_once()

    def test_ollama_cloud_is_supported_provider(self):
        self.assertIn("Ollama Cloud", LLM_PROVIDERS)

    def test_openrouter_is_supported_provider(self):
        self.assertIn("OpenRouter", LLM_PROVIDERS)

    def test_optimize_rejects_invalid_provider(self):
        uploaded = SimpleUploadedFile(
            "x.pdf", b"%PDF", content_type="application/pdf"
        )
        response = self.client.post(
            "/api/resume/optimize",
            data={
                "job_description": "Test",
                "llm_provider": "InvalidProvider",
                "api_key": "k",
                "file": uploaded,
            },
        )
        # 400 = business validation; 422 = request/schema validation
        self.assertIn(
            response.status_code,
            (400, 422),
            msg=f"Expected 400 or 422, got {response.status_code}: {getattr(response, 'content', b'')[:500]}",
        )

    @patch("resume_app.api.optimize_resume_task")
    def test_optimize_rejects_invalid_workflow_steps(self, mock_task):
        uploaded = SimpleUploadedFile(
            "x.pdf", b"%PDF", content_type="application/pdf"
        )
        response = self.client.post(
            "/api/resume/optimize",
            data={
                "job_description": "Test",
                "llm_provider": "OpenAI",
                "api_key": "k",
                "workflow_steps": '["writer", "invalid_step"]',
                "file": uploaded,
            },
        )
        # 400 = business validation; 422 = request/schema validation
        self.assertIn(
            response.status_code,
            (400, 422),
            msg=f"Expected 400 or 422, got {response.status_code}: {getattr(response, 'content', b'')[:500]}",
        )
        if response.status_code == 200:
            mock_task.assert_not_called()


class TaskTestCase(TestCase):
    @patch("resume_app.tasks.get_llm")
    @patch("resume_app.tasks.parse_pdf")
    @patch("resume_app.tasks.create_workflow")
    def test_optimize_resume_task_updates_status_on_success(
        self, mock_create_workflow, mock_parse_pdf, mock_get_llm
    ):
        from .tasks import optimize_resume_task
        from .models import AgentLog

        mock_parse_pdf.return_value = "Resume text"
        mock_get_llm.return_value = MagicMock()
        graph = MagicMock()
        graph.stream.return_value = [
            {"writer": {"optimized_resume": "Optimized", "iteration_count": 1}},
            {"ats_judge": {"ats_score": 80, "feedback": ["ATS: good"]}},
            {"recruiter_judge": {"recruiter_score": 85, "feedback": ["Rec: good"]}},
        ]
        mock_create_workflow.return_value = graph

        resume = UserResume.objects.create(file="test.pdf")
        jd = JobDescription.objects.create(content="JD")
        optimized = OptimizedResume.objects.create(
            original_resume=resume, job_description=jd, status=OptimizedResume.STATUS_QUEUED
        )
        optimize_resume_task(optimized.id, jd.id, "OpenAI", "key")

        optimized.refresh_from_db()
        self.assertEqual(optimized.status, OptimizedResume.STATUS_COMPLETED)
        self.assertEqual(optimized.optimized_content, "Optimized")
        self.assertEqual(optimized.ats_score, 80)
        self.assertEqual(optimized.recruiter_score, 85)
        self.assertGreater(AgentLog.objects.filter(optimized_resume=optimized).count(), 0)


class WriterNodePromptTestCase(TestCase):
    """Writer must pass optimized_resume into the template so multi-step workflows revise the real draft."""

    def test_writer_node_includes_optimized_resume_in_prompt(self):
        from resume_app.agents import writer_node

        captured = {}

        def fake_llm_invoke(llm, messages, max_attempts=3, config=None):
            msg = messages[0]
            captured["prompt"] = getattr(msg, "content", str(msg))
            r = MagicMock()
            r.content = "NEW RESUME OUT"
            r.usage_metadata = None
            return r

        template = (
            "DRAFT:\n{optimized_resume}\nORIG:\n{resume_text}\nJD:\n{job_description}\nFB:\n{feedback}"
        )
        state = {
            "resume_text": "ORIGINAL BODY",
            "job_description": "JD HERE",
            "optimized_resume": "PREVIOUS DRAFT LINE",
            "feedback": ["ATS: fix keywords"],
            "iteration_count": 0,
            "llm": MagicMock(),
            "writer_prompt_template": template,
            "debug": False,
        }

        with patch("resume_app.agents._llm_invoke_with_retry", side_effect=fake_llm_invoke):
            out = writer_node(state)

        self.assertIn("PREVIOUS DRAFT LINE", captured.get("prompt", ""))
        self.assertEqual(out.get("optimized_resume"), "NEW RESUME OUT")
        self.assertEqual(out.get("resume_text"), "NEW RESUME OUT")

    def test_writer_node_puts_latest_draft_in_resume_text_slot_not_pdf(self):
        from resume_app.agents import writer_node

        captured = {}

        def fake_llm_invoke(llm, messages, max_attempts=3, config=None):
            msg = messages[0]
            captured["prompt"] = getattr(msg, "content", str(msg))
            r = MagicMock()
            r.content = "OUT"
            r.usage_metadata = None
            return r

        template = "SRC:\n{source_resume_text}\nDOC:\n{resume_text}\n"
        state = {
            "resume_text": "RAW_PDF_TEXT",
            "source_resume_text": "RAW_PDF_TEXT",
            "job_description": "JD",
            "optimized_resume": "TAILORED_V1",
            "feedback": [],
            "iteration_count": 0,
            "llm": MagicMock(),
            "writer_prompt_template": template,
            "debug": False,
        }

        with patch("resume_app.agents._llm_invoke_with_retry", side_effect=fake_llm_invoke):
            writer_node(state)

        p = captured.get("prompt", "")
        self.assertIn("RAW_PDF_TEXT", p)
        self.assertIn("TAILORED_V1", p)
        self.assertGreater(p.index("TAILORED_V1"), p.index("RAW_PDF_TEXT"))
        self.assertIn("DOC:\nTAILORED_V1", p)


class PipelineStageViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def _create_job_and_entry(self, track="ic"):
        job = JobListing.objects.create(
            source="test",
            external_id="ext-1",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(job_listing=job, track=track)
        return job, pe

    def test_pipeline_save_moves_to_vetting(self):
        job, _pe = self._create_job_and_entry()
        response = self.client.post(
            "/jobs/pipeline/?track=ic",
            data={
                "action": "save",
                "job_id": job.id,
                "track": "ic",
                "next": "/jobs/pipeline/?track=ic",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        pe = PipelineEntry.objects.get(job_listing=job, track="ic")
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)

    def test_vetting_save_moves_to_applying(self):
        job, pe = self._create_job_and_entry()
        pe.move_to_vetting()
        response = self.client.post(
            "/jobs/vetting/?track=ic",
            data={
                "action": "save",
                "job_id": job.id,
                "track": "ic",
                "next": "/jobs/vetting/?track=ic",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.APPLYING)

    def test_applying_save_moves_to_done(self):
        job, pe = self._create_job_and_entry()
        pe.move_to_applying()
        response = self.client.post(
            "/jobs/applying/?track=ic",
            data={
                "action": "save",
                "job_id": job.id,
                "track": "ic",
                "next": "/jobs/applying/?track=ic",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.DONE)

    def test_applying_bulk_optimize_enqueues_task(self):
        job, pe = self._create_job_and_entry()
        job.description = "x" * 60
        job.save(update_fields=["description"])
        pe.move_to_applying()
        with patch("resume_app.tasks.enqueue_applying_resume_optimization_task") as mock_eq:
            response = self.client.post(
                "/jobs/applying/?track=ic",
                data={
                    "action": "bulk_optimize",
                    "job_ids": [str(job.id)],
                    "track": "ic",
                    "next": "/jobs/applying/?track=ic",
                },
            )
        self.assertIn(response.status_code, (302, 303))
        mock_eq.assert_called_once_with([pe.id], force_new=True)


class PipelineResumeEnqueueTestCase(TestCase):
    def test_enqueue_skips_when_queued_exists(self):
        job = JobListing.objects.create(
            source="test",
            external_id="eq-1",
            title="Engineer",
            company_name="ACME",
            description="d" * 60,
        )
        pe = PipelineEntry.objects.create(
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
        )
        ur = UserResume.objects.create(file="r.pdf")
        jd = JobDescription.objects.create(content="c" * 60)
        OptimizedResume.objects.create(
            original_resume=ur,
            job_description=jd,
            pipeline_entry=pe,
            status=OptimizedResume.STATUS_QUEUED,
        )
        result = _enqueue_single_pipeline_resume_optimization(pe.id, force_new=False)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(OptimizedResume.objects.filter(pipeline_entry=pe).count(), 1)

    @patch("resume_app.tasks.optimize_resume_task")
    @patch("resume_app.tasks.decrypt_api_key", return_value="sk-test")
    @patch("resume_app.tasks._resolve_llm_for_pipeline_optimization")
    def test_enqueue_creates_run_with_pipeline_link(
        self, mock_resolve, _mock_decrypt, mock_optimize_task
    ):
        cfg = MagicMock()
        cfg.encrypted_api_key = "enc"
        cfg.default_model = "gpt-4o-mini"
        mock_resolve.return_value = ("OpenAI", cfg)

        job = JobListing.objects.create(
            source="test",
            external_id="eq-2",
            title="Engineer",
            company_name="ACME",
            description="d" * 60,
        )
        pe = PipelineEntry.objects.create(
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
        )
        UserResume.objects.create(file="r2.pdf")

        result = _enqueue_single_pipeline_resume_optimization(pe.id, force_new=False)
        self.assertEqual(result["status"], "ok")
        self.assertIn("optimized_resume_id", result)
        opt = OptimizedResume.objects.get(id=result["optimized_resume_id"])
        self.assertEqual(opt.pipeline_entry_id, pe.id)
        mock_optimize_task.assert_called_once()
        call_kw = mock_optimize_task.call_args.kwargs
        self.assertTrue(call_kw.get("debug"))


class PipelineAutomationTestCase(TestCase):
    def test_pipeline_auto_promotion_moves_to_vetting_and_enqueues_matching(self):
        cfg = AppAutomationSettings.get_solo()
        cfg.pipeline_to_vetting_enabled = True
        cfg.pipeline_preference_margin_min = 5
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-p1",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(job_listing=job, track="ic", stage="")
        JobListingTrackMetrics.objects.create(
            job_listing=job,
            track="ic",
            preference_margin=10,
        )

        with patch("resume_app.tasks.evaluate_vetting_matching_task") as mock_ev:
            n = apply_pipeline_auto_promotions()
        self.assertEqual(n, 1)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)
        mock_ev.assert_called_once()
        args, kwargs = mock_ev.call_args
        self.assertEqual(args[0], [pe.id])
        self.assertIsNone(kwargs.get("matching_prompt"))

    def test_pipeline_auto_promotion_skips_when_disabled(self):
        cfg = AppAutomationSettings.get_solo()
        cfg.pipeline_to_vetting_enabled = False
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-p2",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(job_listing=job, track="ic", stage="")
        JobListingTrackMetrics.objects.create(
            job_listing=job,
            track="ic",
            preference_margin=99,
        )

        with patch("resume_app.tasks.evaluate_vetting_matching_task") as mock_ev:
            n = apply_pipeline_auto_promotions()
        self.assertEqual(n, 0)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, "")
        mock_ev.assert_not_called()

    def test_vetting_auto_promotion_moves_to_applying(self):
        cfg = AppAutomationSettings.get_solo()
        cfg.vetting_to_applying_enabled = True
        cfg.vetting_interview_probability_min = 50
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-v1",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.filter(pk=pe.pk).update(vetting_interview_probability=80)
        pe.refresh_from_db()

        with patch("resume_app.tasks.enqueue_applying_resume_optimization_task") as mock_eq:
            n = apply_vetting_to_applying_promotions()
        self.assertEqual(n, 1)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.APPLYING)
        mock_eq.assert_not_called()

    def test_vetting_auto_promotion_respects_threshold(self):
        cfg = AppAutomationSettings.get_solo()
        cfg.vetting_to_applying_enabled = True
        cfg.vetting_interview_probability_min = 90
        cfg.save()

        job = JobListing.objects.create(
            source="test",
            external_id="auto-v2",
            title="Engineer",
            company_name="ACME",
        )
        pe = PipelineEntry.objects.create(
            job_listing=job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.filter(pk=pe.pk).update(vetting_interview_probability=50)
        pe.refresh_from_db()

        n = apply_vetting_to_applying_promotions()
        self.assertEqual(n, 0)
        pe.refresh_from_db()
        self.assertEqual(pe.stage, PipelineEntry.Stage.VETTING)


class JobDedupeTestCase(TestCase):
    def setUp(self):
        from .models import Track

        Track.ensure_baseline()

    def test_fingerprint_matches_same_description_different_location(self):
        from .job_dedupe import job_listing_fingerprint

        desc = "Same body " * 20
        a = JobListing.objects.create(
            source="t",
            external_id="fp-a",
            title="Role",
            company_name="Co",
            description=desc,
            location="Seattle, WA",
        )
        b = JobListing.objects.create(
            source="t",
            external_id="fp-b",
            title="Role",
            company_name="Co",
            description=desc,
            location="Bellevue, WA",
        )
        self.assertEqual(job_listing_fingerprint(a), job_listing_fingerprint(b))

    def test_dedupe_keeps_higher_focus_after_penalty(self):
        from .job_dedupe import dedupe_pipeline_entries

        desc = "Shared description for dedupe winner test."
        j1 = JobListing.objects.create(
            source="t",
            external_id="dw-1",
            title="Role",
            company_name="Co",
            description=desc,
            location="A",
        )
        j2 = JobListing.objects.create(
            source="t",
            external_id="dw-2",
            title="Role",
            company_name="Co",
            description=desc,
            location="B",
        )
        JobListingTrackMetrics.objects.create(job_listing=j1, track="ic", focus_after_penalty=50)
        JobListingTrackMetrics.objects.create(job_listing=j2, track="ic", focus_after_penalty=90)
        e1 = PipelineEntry.objects.create(job_listing=j1, track="ic", stage="")
        e2 = PipelineEntry.objects.create(job_listing=j2, track="ic", stage="")
        dedupe_pipeline_entries(track_slug="ic", stage="pipeline", include_done=False)
        e1.refresh_from_db()
        e2.refresh_from_db()
        self.assertIsNotNone(e1.removed_at)
        self.assertIsNone(e2.removed_at)

    def test_dedupe_respects_stage_scope(self):
        from .job_dedupe import dedupe_pipeline_entries

        desc = "Stage scope " * 30
        j1 = JobListing.objects.create(
            source="t",
            external_id="st-1",
            title="R",
            company_name="C",
            description=desc,
        )
        j2 = JobListing.objects.create(
            source="t",
            external_id="st-2",
            title="R",
            company_name="C",
            description=desc,
        )
        PipelineEntry.objects.create(
            job_listing=j1,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        PipelineEntry.objects.create(
            job_listing=j2,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )
        r0 = dedupe_pipeline_entries(track_slug="ic", stage="pipeline", include_done=False)
        self.assertEqual(r0["entries_removed"], 0)
        r1 = dedupe_pipeline_entries(track_slug="ic", stage="vetting", include_done=False)
        self.assertEqual(r1["entries_removed"], 1)
