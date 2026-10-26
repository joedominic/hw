"""Tests for on-demand cover letter and interview prep generation."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase

from .job_prep import (
    InterviewPrepResult,
    JobPrepError,
    generate_cover_letter,
    generate_interview_prep,
    interview_prep_to_markdown,
    resolve_interview_prep_inputs,
)
from .models import (
    JobDescription,
    JobListing,
    OptimizedResume,
    PipelineEntry,
    UserResume,
)
from .test_utils import create_user


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
        self.user = create_user("jobprep")
        self.job = JobListing.objects.create(
            source="test",
            external_id="jobprep-1",
            title="Engineer",
            company_name="Acme",
            description="Need Python and Django.",
            url="https://example.com/job/1",
        )
        self.entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.DONE,
        )
        self.jd = JobDescription.objects.create(content="Need Python.")
        self.resume = UserResume.objects.create(
            owner=self.user,
            file="resumes/test.pdf",
            original_filename="test.pdf",
            track="ic",
            is_library=True,
        )
        self.opt = OptimizedResume.objects.create(
            owner=self.user,
            original_resume=self.resume,
            job_description=self.jd,
            optimized_content="Tailored resume for Acme.",
            status=OptimizedResume.STATUS_COMPLETED,
            pipeline_entry=self.entry,
            cover_letter="Dear Acme team…",
        )

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
        self.user = create_user("coverletter")
        self.jd = JobDescription.objects.create(content="Build APIs.")
        self.resume = UserResume.objects.create(
            owner=self.user, file="r.pdf", original_filename="r.pdf"
        )
        self.opt = OptimizedResume.objects.create(
            owner=self.user,
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
        self.user = create_user("interviewprep")
        self.job = JobListing.objects.create(
            source="test",
            external_id="interviewprep-1",
            title="Engineer",
            company_name="Acme",
            description="Python role at Acme.",
        )
        self.entry = PipelineEntry.objects.create(
            owner=self.user,
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
        resume = UserResume.objects.create(
            owner=self.user, file="r.pdf", original_filename="r.pdf", track="ic", is_library=True
        )
        OptimizedResume.objects.create(
            owner=self.user,
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


class CoverLetterSizingAndExportTests(TestCase):
    def setUp(self):
        self.user = create_user("clexport")
        self.job = JobListing.objects.create(
            source="dice",
            external_id="clexport-1",
            title="Lead Engineer",
            company_name="Innovate Corp",
            description="Need Python and cloud architect.",
            url="https://example.com/job/1",
        )
        self.entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.APPLYING,
        )
        self.jd = JobDescription.objects.create(content="Need Python.")
        self.resume = UserResume.objects.create(
            owner=self.user, file="r.pdf", original_filename="r.pdf", track="ic", is_library=True
        )
        self.opt = OptimizedResume.objects.create(
            owner=self.user,
            original_resume=self.resume,
            job_description=self.jd,
            optimized_content="Tailored resume for Innovate Corp.",
            status=OptimizedResume.STATUS_COMPLETED,
            pipeline_entry=self.entry,
            cover_letter="Dear Innovate Corp Team,\n\nI am writing to express my strong interest in the Lead Engineer position.\n\nSincerely,\nCandidate",
        )

    @patch("resume_app.job_prep._llm_invoke_with_retry")
    def test_generate_cover_letter_with_length_short(self, mock_invoke):
        mock_invoke.return_value = MagicMock(content="Short punchy cover letter.")
        letter, prompt = generate_cover_letter(self.opt, llm=MagicMock(), length="short")
        self.assertEqual(letter, "Short punchy cover letter.")
        self.assertIn("Keep the cover letter short", prompt)

    @patch("resume_app.job_prep._llm_invoke_with_retry")
    def test_generate_cover_letter_with_length_detailed(self, mock_invoke):
        mock_invoke.return_value = MagicMock(content="Detailed cover letter.")
        letter, prompt = generate_cover_letter(self.opt, llm=MagicMock(), length="detailed")
        self.assertEqual(letter, "Detailed cover letter.")
        self.assertIn("In-depth, comprehensive cover letter", prompt)

    @patch("resume_app.job_prep._llm_invoke_with_retry")
    def test_generate_cover_letter_shorter_references_existing_draft(self, mock_invoke):
        mock_invoke.return_value = MagicMock(content="Condensed letter.")
        letter, prompt = generate_cover_letter(self.opt, llm=MagicMock(), length="shorter")
        self.assertEqual(letter, "Condensed letter.")
        self.assertIn("Current Draft Cover Letter to scale:", prompt)
        self.assertIn("Condense and shorten", prompt)

    def test_export_cover_letter_pdf_success(self):
        from django.test import Client
        client = Client()
        client.force_login(self.user)

        res = client.get(f"/api/resume/export/{self.opt.id}/cover-letter/pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertIn("cover_letter", res["Content-Disposition"])
        body = res.getvalue() if hasattr(res, "getvalue") else b"".join(res.streaming_content)
        self.assertTrue(body.startswith(b"%PDF"))

    def test_export_cover_letter_docx_success(self):
        from django.test import Client
        client = Client()
        client.force_login(self.user)

        res = client.get(f"/api/resume/export/{self.opt.id}/cover-letter/docx")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn("cover_letter", res["Content-Disposition"])
        body = res.getvalue() if hasattr(res, "getvalue") else b"".join(res.streaming_content)
        self.assertTrue(len(body) > 100)

    def test_export_cover_letter_missing_returns_404(self):
        from django.test import Client
        client = Client()
        client.force_login(self.user)

        self.opt.cover_letter = ""
        self.opt.save(update_fields=["cover_letter"])

        res_pdf = client.get(f"/api/resume/export/{self.opt.id}/cover-letter/pdf")
        self.assertEqual(res_pdf.status_code, 404)

        res_docx = client.get(f"/api/resume/export/{self.opt.id}/cover-letter/docx")
        self.assertEqual(res_docx.status_code, 404)


class PipelineInterviewPrepCsrfTests(TestCase):
    def setUp(self):
        from django.test import Client

        self.user = create_user("prep_csrf_user")
        self.job = JobListing.objects.create(
            source="dice",
            external_id="prep-csrf-1",
            title="Senior Dev",
            company_name="Acme",
            description="Python engineer with Django experience.",
        )
        self.entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.DONE,
        )
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def test_generate_interview_prep_requires_csrf_when_enforced(self):
        url = f"/api/resume/jobs/pipeline-entry/{self.entry.id}/generate-interview-prep"
        resp = self.client.post(url, data=json.dumps({}), content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("CSRF", resp.content.decode())

    @patch("resume_app.job_prep.generate_interview_prep")
    @patch("resume_app.jobs_api._get_llm_from_request")
    def test_generate_interview_prep_succeeds_with_csrf_token(self, mock_llm, mock_gen):
        from django.middleware.csrf import get_token
        from django.test import RequestFactory

        mock_gen.return_value = ("{}", "# Markdown", "Prompt")
        url = f"/api/resume/jobs/pipeline-entry/{self.entry.id}/generate-interview-prep"
        req = RequestFactory().get("/")
        csrf_token = get_token(req)
        self.client.cookies["csrftoken"] = csrf_token
        resp = self.client.post(
            url,
            data=json.dumps({}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["markdown"], "# Markdown")


class MultiRoundInterviewPrepTests(TestCase):
    def setUp(self):
        self.user = create_user("preprounds")
        self.job = JobListing.objects.create(
            source="test",
            external_id="prep-rounds-1",
            title="Senior Backend Engineer",
            company_name="CloudCorp",
            description="Build scalable distributed systems with Python, Django, and Kubernetes.",
        )
        self.entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.DONE,
        )
        self.jd = JobDescription.objects.create(content=self.job.description)
        self.resume = UserResume.objects.create(
            owner=self.user,
            file="resume.pdf",
            original_filename="resume.pdf",
            track="ic",
            is_library=True,
        )
        self.opt = OptimizedResume.objects.create(
            owner=self.user,
            original_resume=self.resume,
            job_description=self.jd,
            optimized_content="Staff Software Engineer with deep Python, distributed systems, and AWS experience.",
            status=OptimizedResume.STATUS_COMPLETED,
            pipeline_entry=self.entry,
        )
        self.client = Client()
        self.client.force_login(self.user)

    @patch("resume_app.job_prep._llm_invoke_with_retry")
    def test_generate_interview_prep_for_different_rounds(self, mock_invoke):
        from resume_app.job_prep import generate_interview_prep, get_all_interview_preps

        # 1. Generate Recruiter round
        recruiter_payload = {
            "likely_questions": ["Tell me about yourself.", "Why CloudCorp?", "What are your salary expectations?"],
            "themes_to_emphasize": ["Communication", "Career narrative"],
            "suggested_answers": [
                {
                    "question": "Why CloudCorp?",
                    "talking_points": ["Excited about scalable cloud infrastructure"],
                    "resume_evidence": ["Staff Software Engineer with deep Python"],
                    "sample_answer": "I have spent 10 years building distributed backend systems, and CloudCorp is at the forefront of cloud reliability."
                }
            ],
            "questions_to_ask": ["What does the hiring timeline look like?"]
        }
        mock_invoke.return_value = MagicMock(content=json.dumps(recruiter_payload))
        stored_recruiter, md_recruiter, prompt_recruiter = generate_interview_prep(
            self.entry, llm=MagicMock(), interview_type="recruiter"
        )
        self.assertIn("Why CloudCorp?", stored_recruiter)
        self.assertIn("Sample response", md_recruiter)
        self.assertIn("CloudCorp is at the forefront", md_recruiter)
        self.assertIn("Questions to ask the interviewer", md_recruiter)
        self.assertIn("What does the hiring timeline look like?", md_recruiter)
        self.assertIn("RECRUITER SCREEN", prompt_recruiter.upper())
        self.assertIn("Why CloudCorp?", prompt_recruiter)

        # 2. Generate Technical round without overwriting recruiter round
        tech_payload = {
            "likely_questions": ["Explain distributed caching trade-offs."],
            "themes_to_emphasize": ["Scalability", "Kubernetes"],
            "suggested_answers": [
                {
                    "question": "Explain distributed caching trade-offs.",
                    "talking_points": ["Cache invalidation and latency"],
                    "resume_evidence": ["Distributed systems experience"],
                    "sample_answer": "In distributed caching, the primary trade-off is between consistency and latency."
                }
            ],
            "questions_to_ask": ["How is technical debt prioritized?"]
        }
        mock_invoke.return_value = MagicMock(content=json.dumps(tech_payload))
        stored_tech, md_tech, prompt_tech = generate_interview_prep(
            self.entry, llm=MagicMock(), interview_type="technical"
        )
        self.assertIn("distributed caching", stored_tech)
        self.assertIn("TECHNICAL / ARCHITECTURE", prompt_tech.upper())

        # 3. Verify both rounds are preserved in database
        self.entry.refresh_from_db()
        all_preps = get_all_interview_preps(self.entry.interview_prep)
        self.assertIn("recruiter", all_preps)
        self.assertIn("technical", all_preps)
        self.assertIn("Tell me about yourself.", all_preps["recruiter"]["content"])
        self.assertIn("distributed caching", all_preps["technical"]["content"])

    def test_legacy_format_backward_compatibility(self):
        from resume_app.job_prep import get_all_interview_preps

        legacy_json = json.dumps({
            "likely_questions": ["Tell me about a conflict."],
            "themes_to_emphasize": ["Collaboration"],
            "suggested_answers": [],
        })
        preps = get_all_interview_preps(legacy_json)
        self.assertIn("behavioral", preps)
        self.assertIn("Tell me about a conflict.", preps["behavioral"]["content"])

    @patch("resume_app.job_prep.generate_interview_prep")
    @patch("resume_app.jobs_api._get_llm_from_request")
    def test_api_generate_and_get_round_specific_prep(self, mock_llm, mock_gen):
        mock_gen.return_value = (
            json.dumps({"likely_questions": ["How do you handle conflict?"]}),
            "## Likely questions\n1. How do you handle conflict?",
            "Prompt text"
        )
        # Generate behavioral prep via API
        post_url = f"/api/resume/jobs/pipeline-entry/{self.entry.id}/generate-interview-prep"
        resp = self.client.post(
            post_url,
            data=json.dumps({"interview_type": "behavioral"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["interview_type"], "behavioral")
        self.assertIn("How do you handle conflict?", data["markdown"])

        # GET interview prep via API
        get_url = f"/api/resume/jobs/pipeline-entry/{self.entry.id}/interview-prep?interview_type=behavioral"
        get_resp = self.client.get(get_url)
        self.assertEqual(get_resp.status_code, 200)
        get_data = get_resp.json()
        self.assertEqual(get_data["interview_type"], "behavioral")



