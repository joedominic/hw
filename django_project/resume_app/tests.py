from django.test import TestCase, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch, MagicMock
from .models import UserResume, JobDescription, OptimizedResume
from .services import parse_pdf, PDFParseError
from .llm_services import LLM_PROVIDERS
import os

class ModelsTestCase(TestCase):
    def test_model_creation(self):
        resume = UserResume.objects.create(file="test.pdf")
        jd = JobDescription.objects.create(content="Test JD")
        optimized = OptimizedResume.objects.create(original_resume=resume, job_description=jd)
        self.assertEqual(optimized.status, OptimizedResume.STATUS_QUEUED)

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


class APITestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def test_status_404_for_invalid_resume_id(self):
        response = self.client.get("/api/resume/status/99999/")
        self.assertEqual(response.status_code, 404)

    @patch("resume_app.api.run_optimize_resume_task")
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

    @patch("resume_app.api.run_optimize_resume_task")
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
