"""Regression tests for the priority review fixes."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from resume_app.jobs_api import jobs_fetch_description
from resume_app.llm.factory import _normalize_ollama_local_host, get_llm
from resume_app.models import AppAutomationSettings, JobListing, PipelineEntry
from resume_app.schemas import FetchJobDescriptionRequest
from resume_app.test_utils import create_user

User = get_user_model()


class SharedJobListingWriteTests(TestCase):
    def setUp(self):
        self.owner = create_user("listing-owner")
        self.other = create_user("listing-other")
        self.job = JobListing.objects.create(
            source="dice",
            external_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            title="Original title",
            company_name="Original Co",
            description="short",
            url="",
        )
        PipelineEntry.objects.create(
            owner=self.owner,
            job_listing=self.job,
            track="ic",
            stage=PipelineEntry.Stage.VETTING,
        )

    def _request(self, user):
        request = RequestFactory().post("/api/resume/jobs/fetch-description")
        request.user = user
        return request

    @patch("resume_app.sourcing.clients.dice_client.fetch_dice_job_detail")
    @patch("resume_app.sourcing.clients.dice_client.enrich_dice_job_listing_description", return_value="")
    def test_unowned_listing_is_not_updated(self, _enrich, fetch_detail):
        fetch_detail.return_value = {
            "description": "Fetched from the pasted URL only.",
            "title": "Attacker title",
            "company_name": "Attacker Co",
            "location": "Remote",
            "job_url": "https://www.dice.com/job-detail/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
        payload = FetchJobDescriptionRequest(
            url="https://www.dice.com/job-detail/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            job_listing_id=self.job.id,
        )
        result = jobs_fetch_description(self._request(self.other), payload)
        self.job.refresh_from_db()
        self.assertEqual(self.job.url, "")
        self.assertEqual(self.job.title, "Original title")
        self.assertEqual(self.job.description, "short")
        self.assertIn("Fetched from the pasted URL", result.description)
        _enrich.assert_not_called()

    @patch("resume_app.sourcing.clients.dice_client.fetch_dice_job_detail")
    @patch("resume_app.sourcing.clients.dice_client.enrich_dice_job_listing_description", return_value="")
    def test_owned_listing_does_not_store_the_request_url(self, _enrich, fetch_detail):
        fetch_detail.return_value = {
            "description": "Official text was not persisted from the request URL.",
            "title": "Still original",
            "company_name": "Original Co",
            "location": "",
            "job_url": "https://www.dice.com/job-detail/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
        payload = FetchJobDescriptionRequest(
            url="https://www.dice.com/job-detail/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            job_listing_id=self.job.id,
        )
        jobs_fetch_description(self._request(self.owner), payload)
        self.job.refresh_from_db()
        self.assertEqual(self.job.url, "")
        self.assertEqual(self.job.title, "Original title")


class OllamaHostGuardTests(TestCase):
    def test_loopback_and_private_lan_are_allowed(self):
        self.assertEqual(
            _normalize_ollama_local_host("127.0.0.1"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            _normalize_ollama_local_host("192.168.1.10:11434"),
            "http://192.168.1.10:11434",
        )

    def test_metadata_and_public_addresses_are_rejected(self):
        for raw in ("http://169.254.169.254", "http://8.8.8.8", "http://metadata.google.internal"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    _normalize_ollama_local_host(raw)

    def test_get_llm_does_not_pin_process_environment(self):
        os.environ.pop("OLLAMA_API_BASE", None)
        get_llm("Ollama Local", api_key="http://127.0.0.1:11434", model="llama3.2")
        self.assertNotIn("OLLAMA_API_BASE", os.environ)


class SkillRadarProfileParamTests(TestCase):
    def setUp(self):
        self.user = create_user("radar-tenant")
        self.job = JobListing.objects.create(
            source="indeed",
            external_id="radar-1",
            title="Engineer",
            company_name="Acme",
            description="Python",
            url="https://example.com/jobs/1",
        )

    @patch("resume_app.skill_radar.SkillRadarService.analyze")
    @patch("resume_app.pipeline_board._resolve_cockpit_bound_resume", return_value=None)
    def test_profile_query_is_the_search_profile_slug(self, resolve_resume, analyze):
        from resume_app.pipeline_board import cockpit_skill_radar_api

        analyze.return_value = {
            "match_score": 80,
            "interview_probability": 70,
            "core_competencies": [],
            "stretch_skills": [],
            "fit_summary": "ok",
            "source": "local_slm",
        }
        request = RequestFactory().get(
            f"/jobs/cockpit/api/skill-radar/?job_id={self.job.id}&profile=staff-ml&force=1"
        )
        request.user = self.user
        response = cockpit_skill_radar_api(request)
        self.assertEqual(response.status_code, 200)
        resolve_resume.assert_called_once()
        self.assertEqual(resolve_resume.call_args.args[1], "staff-ml")


class TenantPipelinePolicyTests(TestCase):
    def test_tenant_can_save_own_pipeline_policies(self):
        user = create_user("policy-tenant")
        self.client.force_login(user)
        response = self.client.post(
            reverse("career_cockpit"),
            {
                "action": "update_pipeline_retention",
                "cleanup_pipeline_retention_days": "14",
                "pipeline_purge_match_score_max": "30",
            },
        )
        self.assertEqual(response.status_code, 302)
        cfg = AppAutomationSettings.get_for_user(user)
        self.assertEqual(cfg.cleanup_pipeline_retention_days, 14)
        self.assertEqual(cfg.pipeline_purge_match_score_max, 30)


class PromptStudioCsrfTests(TestCase):
    def test_save_api_rejects_missing_csrf_token(self):
        staff = create_user("prompt-staff")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(staff)
        response = client.post(
            reverse("prompt_studio_save_api"),
            data=json.dumps({"prompt_key": "writer", "system_prompt": "x", "user_prompt": "y"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
