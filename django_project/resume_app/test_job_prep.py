"""Tests for on-demand cover letter and interview prep generation."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from .job_prep import (
    InterviewPrepResult,
    JobPrepError,
    generate_cover_letter,
    generate_interview_prep,
    interview_prep_to_markdown,
    resolve_interview_prep_inputs,
)
from .models import (
    ApplicationAttempt,
    JobDescription,
    JobListing,
    OptimizedResume,
    PipelineEntry,
    UserResume,
)


class InterviewPrepMarkdownTests(TestCase):
    def test_renders_json_as_markdown(self):
        payload = InterviewPrepResult(
            likely_questions=["Tell me about yourself."],
            themes_to_emphasize=["Python"],
            suggested_answers=[
                {
                    "question": "Tell me about yourself.",
                    "talking_points": ["Backend focus"],
                    "resume_evidence": ["Built APIs at Acme"],
                }
            ],
        )
        md = interview_prep_to_markdown(payload.model_dump_json())
        self.assertIn("Likely questions", md)
        self.assertIn("Tell me about yourself", md)
        self.assertIn("Python", md)

    def test_plain_text_passthrough(self):
        self.assertEqual(interview_prep_to_markdown("Raw notes"), "Raw notes")


class ResolveInterviewPrepInputsTests(TestCase):
    def setUp(self):
        self.job = JobListing.objects.create(
            title="Engineer",
            company_name="Acme",
            description="Need Python and Django.",
            url="https://example.com/job/1",
        )
        self.entry = PipelineEntry.objects.create(
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.DONE,
        )
        self.jd = JobDescription.objects.create(content="Need Python.")
        self.resume = UserResume.objects.create(
            file="resumes/test.pdf",
            original_filename="test.pdf",
            track="ic",
            is_library=True,
        )
        self.opt = OptimizedResume.objects.create(
            original_resume=self.resume,
            job_description=self.jd,
            optimized_content="Tailored resume for Acme.",
            status=OptimizedResume.STATUS_COMPLETED,
            pipeline_entry=self.entry,
            cover_letter="Dear Acme team…",
        )

    def test_prefers_succeeded_attempt_resume(self):
        ApplicationAttempt.objects.create(
            pipeline_entry=self.entry,
            optimized_resume=self.opt,
            status=ApplicationAttempt.Status.SUCCEEDED,
        )
        inputs = resolve_interview_prep_inputs(self.entry)
        self.assertEqual(inputs.resume_text, "Tailored resume for Acme.")
        self.assertIn("Python", inputs.job_description)

    def test_falls_back_to_optimized_resume(self):
        inputs = resolve_interview_prep_inputs(self.entry)
        self.assertEqual(inputs.resume_text, "Tailored resume for Acme.")

    @patch("resume_app.job_prep.parse_pdf", return_value="Library resume text.")
    def test_falls_back_to_library_resume(self, _mock_pdf):
        OptimizedResume.objects.filter(pk=self.opt.pk).delete()
        inputs = resolve_interview_prep_inputs(self.entry)
        self.assertEqual(inputs.resume_text, "Library resume text.")


class GenerateCoverLetterTests(TestCase):
    def setUp(self):
        self.jd = JobDescription.objects.create(content="Build APIs.")
        self.resume = UserResume.objects.create(file="r.pdf", original_filename="r.pdf")
        self.opt = OptimizedResume.objects.create(
            original_resume=self.resume,
            job_description=self.jd,
            optimized_content="Senior engineer with Python.",
            status=OptimizedResume.STATUS_COMPLETED,
        )

    def test_requires_completed_optimization(self):
        self.opt.status = OptimizedResume.STATUS_RUNNING
        self.opt.save(update_fields=["status"])
        with self.assertRaises(JobPrepError):
            generate_cover_letter(self.opt, llm=MagicMock())

    @patch("resume_app.job_prep._llm_invoke_with_retry")
    def test_persists_cover_letter(self, mock_invoke):
        mock_invoke.return_value = MagicMock(content="Dear hiring manager, …")
        letter, _prompt = generate_cover_letter(self.opt, llm=MagicMock())
        self.assertIn("Dear hiring manager", letter)
        self.opt.refresh_from_db()
        self.assertEqual(self.opt.cover_letter, letter)
        self.assertIsNotNone(self.opt.cover_letter_generated_at)


class GenerateInterviewPrepTests(TestCase):
    def setUp(self):
        self.job = JobListing.objects.create(
            title="Engineer",
            company_name="Acme",
            description="Python role at Acme.",
        )
        self.entry = PipelineEntry.objects.create(
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
        )

    def test_done_only_guard(self):
        with self.assertRaises(JobPrepError):
            generate_interview_prep(self.entry, llm=MagicMock())

    @patch("resume_app.job_prep._llm_invoke_with_retry")
    def test_persists_interview_prep_json(self, mock_invoke):
        payload = {
            "likely_questions": ["Why this role?"],
            "themes_to_emphasize": ["Python"],
            "suggested_answers": [],
        }
        mock_invoke.return_value = MagicMock(content=json.dumps(payload))
        self.entry.stage = PipelineEntry.Stage.DONE
        self.entry.save(update_fields=["stage"])
        jd = JobDescription.objects.create(content="Python role.")
        resume = UserResume.objects.create(file="r.pdf", original_filename="r.pdf", track="ic", is_library=True)
        OptimizedResume.objects.create(
            original_resume=resume,
            job_description=jd,
            optimized_content="Python engineer.",
            status=OptimizedResume.STATUS_COMPLETED,
            pipeline_entry=self.entry,
        )
        stored, md, _prompt = generate_interview_prep(self.entry, llm=MagicMock())
        self.assertIn("Why this role?", stored)
        self.assertIn("Likely questions", md)
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.interview_prep)
        self.assertIsNotNone(self.entry.interview_prep_generated_at)
