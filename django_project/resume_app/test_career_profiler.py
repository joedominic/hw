import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from resume_app.models import SearchProfile, Track, UserResume, JobSearchTask, CareerProfileAnalysis
from resume_app.career_profiler import (
    analyze_resume_for_career_archetypes,
    provision_career_tracks,
    extract_text_from_user_resume,
    _generate_heuristic_fallback,
)

User = get_user_model()


class CareerProfilerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="prof_user", password="password123")

    def test_heuristic_fallback_non_tech_nursing(self):
        resume_text = """
        Jane Doe, BSN, RN
        Clinical Nurse Supervisor with 8 years of hospital emergency and intensive care unit (ICU) experience.
        Specializes in patient triage, clinical compliance, charge nurse scheduling, and EHR systems.
        """
        result = analyze_resume_for_career_archetypes(resume_text, user=self.user)
        self.assertIn("candidate_summary", result)
        self.assertEqual(result["candidate_summary"]["domain"], "Healthcare & Clinical Operations")
        archetypes = result["archetypes"]
        self.assertEqual(len(archetypes), 3)
        self.assertIn("Nurse", archetypes[0]["title"])
        self.assertEqual(archetypes[0]["type"], "core")
        self.assertEqual(archetypes[1]["type"], "stretch")
        self.assertEqual(archetypes[2]["type"], "adjacent")

    def test_heuristic_fallback_non_tech_finance(self):
        resume_text = """
        John Smith, CPA
        Senior Financial Analyst with 6 years experience in FP&A, financial modeling, budgeting, and general ledger GAAP reporting.
        """
        result = analyze_resume_for_career_archetypes(resume_text, user=self.user)
        self.assertEqual(result["candidate_summary"]["domain"], "Corporate Finance & Accounting")
        archetypes = result["archetypes"]
        self.assertIn("Financial Analyst", archetypes[0]["title"])
        self.assertTrue(archetypes[0]["default_active"])
        self.assertFalse(archetypes[2]["default_active"])

    def test_heuristic_fallback_tech_software(self):
        resume_text = """
        Alex Rivera
        Staff Software Engineer with 10 years experience building distributed systems, Kubernetes platforms, AWS, and Python microservices.
        """
        result = analyze_resume_for_career_archetypes(resume_text, user=self.user)
        self.assertEqual(result["candidate_summary"]["domain"], "Software & Cloud Engineering")
        self.assertIn("Engineer", result["archetypes"][0]["title"])

    def test_provision_career_tracks_with_relocation_and_schedule(self):
        resume = UserResume.objects.create(
            owner=self.user,
            original_filename="jane_resume.pdf",
            is_library=True,
        )

        tracks_config = [
            {
                "title": "Clinical Nurse Manager",
                "slug": "clinical-nurse-manager",
                "search_terms": ["Clinical Nurse Manager", "Charge Nurse"],
                "work_mode": "relocation",  # Open to relocation -> nationwide (location="")
                "location": "",
                "site_names": ["indeed", "linkedin"],
                "schedule_interval": "daily",
                "schedule_time": "08:30",
                "enabled": True,
            },
            {
                "title": "Director of Nursing Services",
                "slug": "director-of-nursing",
                "search_terms": ["Director of Nursing"],
                "work_mode": "metro",
                "location": "Dallas, TX",
                "site_names": ["indeed"],
                "schedule_interval": "off",
                "enabled": True,
            },
            {
                "title": "Healthcare Consultant",
                "slug": "healthcare-consultant",
                "enabled": False,  # disabled track
            },
        ]

        provisioned = provision_career_tracks(self.user, tracks_config, resume_id=resume.id)
        self.assertEqual(len(provisioned), 2)

        # Verify Track 1: relocation = no location restriction
        sp1 = SearchProfile.objects.for_user(self.user).get(slug="clinical-nurse-manager")
        self.assertEqual(sp1.location, "")
        self.assertEqual(sp1.schedule_interval, "daily")
        self.assertTrue(sp1.schedule_is_active)
        self.assertEqual(sp1.resume_id, resume.id)

        # Verify Huey task was created for Track 1
        task = JobSearchTask.objects.for_user(self.user).filter(track="clinical-nurse-manager").first()
        self.assertIsNotNone(task)
        self.assertFalse(task.location)

        # Verify Track 2: specific metro
        sp2 = SearchProfile.objects.for_user(self.user).get(slug="director-of-nursing")
        self.assertEqual(sp2.location, "Dallas, TX")
        self.assertEqual(sp2.schedule_interval, "off")
        self.assertFalse(sp2.schedule_is_active)

    def test_career_profiler_page_view(self):
        self.client.force_login(self.user)
        # Without saved analysis: displays on-demand prompt
        resp = self.client.get(reverse("career_profiler"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Discover your next career tracks")
        self.assertContains(resp, "Ready to profile this resume")

        # Create resume and saved analysis
        resume = UserResume.objects.create(
            owner=self.user,
            file=SimpleUploadedFile("finance.txt", b"CFO Finance Director with 10 years experience."),
            original_filename="finance.txt",
            is_library=True,
        )
        saved_data = {
            "status": "success",
            "resume_id": resume.id,
            "detected_industry": "Corporate Finance & Accounting",
            "detected_seniority": "Director / Executive",
            "archetypes": [
                {
                    "title": "Corporate Controller",
                    "type": "core",
                    "confidence_label": "Core Fit",
                },
                {
                    "title": "Vice President of Finance",
                    "type": "stretch",
                    "confidence_label": "Next Level / Promotion",
                }
            ],
        }
        CareerProfileAnalysis.objects.create(
            owner=self.user,
            resume=resume,
            analysis_data=saved_data,
        )

        resp2 = self.client.get(f"{reverse('career_profiler')}?resume_id={resume.id}")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.context["saved_analysis"], saved_data)
        self.assertContains(resp2, "Vice President of Finance")

    def test_career_profiler_analyze_api(self):
        self.client.force_login(self.user)
        resume = UserResume.objects.create(
            owner=self.user,
            file=SimpleUploadedFile("finance.txt", b"Chief Financial Officer. Managed $50M P&L, GAAP audits, SEC filings."),
            original_filename="finance.txt",
            is_library=True,
        )
        # First call: triggers analysis and stores CareerProfileAnalysis
        resp = self.client.post(
            reverse("career_profiler_analyze_api"),
            data=json.dumps({"resume_id": resume.id, "force_refresh": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["detected_industry"], "Corporate Finance & Accounting")
        self.assertGreaterEqual(len(data["archetypes"]), 1)

        # Verify CareerProfileAnalysis record was persisted in database
        persisted = CareerProfileAnalysis.objects.for_user(self.user).filter(resume=resume).first()
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.analysis_data["detected_industry"], "Corporate Finance & Accounting")

        # Verify next-level role exists in archetypes
        stretch_archetype = next((a for a in data["archetypes"] if a.get("type") == "stretch" or "Next Level" in a.get("confidence_label", "")), None)
        self.assertIsNotNone(stretch_archetype)
        self.assertEqual(stretch_archetype["confidence_label"], "Next Level / Promotion")

        # Second call without force_refresh: should hit cache directly
        resp_cached = self.client.post(
            reverse("career_profiler_analyze_api"),
            data=json.dumps({"resume_id": resume.id, "force_refresh": False}),
            content_type="application/json",
        )
        self.assertEqual(resp_cached.status_code, 200)
        self.assertEqual(resp_cached.json()["detected_industry"], "Corporate Finance & Accounting")

    def test_career_profiler_provision_api(self):
        self.client.force_login(self.user)
        resume = UserResume.objects.create(
            owner=self.user,
            file=SimpleUploadedFile("resume.txt", b"Software Engineer with 8 years of React and Python experience."),
            original_filename="resume.txt",
            is_library=True,
        )
        payload = {
            "resume_id": resume.id,
            "tracks": [
                {
                    "title": "Principal Architect",
                    "slug": "principal-architect",
                    "primary_search_term": "Principal Architect",
                    "work_mode": "everywhere",
                    "location": "",
                    "site_names": ["linkedin", "indeed"],
                    "schedule_interval": "daily",
                    "schedule_time": "08:30",
                    "enabled": True,
                }
            ],
        }
        resp = self.client.post(
            reverse("career_profiler_provision_api"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("/jobs/cockpit/", data["redirect_url"])
        self.assertTrue(SearchProfile.objects.for_user(self.user).filter(slug="principal-architect").exists())

    def test_career_profiler_prompt_library_integration(self):
        from .prompt_store import get_effective_prompts, get_system_prompt_profile, resolve_prompt_parts

        # Make user staff so they can access prompt library
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)

        # GET Prompt Library
        resp = self.client.get(reverse("prompt_library"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Career Profiler")
        self.assertContains(resp, "prompt_career_profiler_system")
        self.assertContains(resp, "prompt_career_profiler_user")

        # POST custom Career Profiler prompt
        post_data = {
            "action": "save_prompts",
            "prompt_career_profiler_system": "Custom Career Profiler System Instruction",
            "prompt_career_profiler_user": "Custom Resume Analysis: {resume_text}",
        }
        post_resp = self.client.post(reverse("prompt_library"), data=post_data, follow=True)
        self.assertEqual(post_resp.status_code, 200)

        # Verify resolved prompt parts
        profile = get_system_prompt_profile()
        sys_t, usr_t, leg_t = resolve_prompt_parts(profile, "career_profiler")
        self.assertEqual(sys_t, "Custom Career Profiler System Instruction")
        self.assertEqual(usr_t, "Custom Resume Analysis: {resume_text}")
        self.assertIsNone(leg_t)

    def test_career_profiler_preview_matches_api(self):
        from .models import JobListing

        self.client.force_login(self.user)
        resume = UserResume.objects.create(
            owner=self.user,
            file=SimpleUploadedFile("nursing_leader.txt", b"Director of Nursing Services with 12 years of clinical operations and hospital management experience."),
            original_filename="nursing_leader.txt",
            is_library=True,
        )

        # Create test job listings
        JobListing.objects.create(
            source="indeed",
            external_id="preview-job-1",
            title="Director of Nursing",
            company_name="Dallas Memorial Hospital",
            location="Dallas, TX",
            description="We are seeking an experienced Director of Nursing to lead clinical operations and nursing staff.",
        )
        JobListing.objects.create(
            source="linkedin",
            external_id="preview-job-2",
            title="Clinical Nurse Specialist",
            company_name="Texas Health",
            location="Remote",
            description="Clinical specialist overseeing patient care standards.",
        )

        # 1. Test empty search_term returns 400
        resp_bad = self.client.post(
            reverse("career_profiler_preview_matches_api"),
            data=json.dumps({"search_term": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp_bad.status_code, 400)

        # 2. Test valid preview search with mocked fetch_jobs to prevent external network calls
        from unittest.mock import patch
        with patch("resume_app.job_sources.fetch_jobs", return_value=[]):
            payload = {
                "search_term": "Director of Nursing",
                "location": "Dallas, TX",
                "resume_id": resume.id,
                "site_names": ["indeed", "linkedin"],
                "results_wanted": 10,
            }
            resp = self.client.post(
                reverse("career_profiler_preview_matches_api"),
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["search_term"], "Director of Nursing")
            self.assertGreater(data["total_found"], 0)
            self.assertIsInstance(data["matches"], list)

            first_match = data["matches"][0]
            self.assertEqual(first_match["title"], "Director of Nursing")
            self.assertEqual(first_match["company"], "Dallas Memorial Hospital")
            self.assertGreater(first_match["match_score"], 50)
            self.assertIn("snippet", first_match)



