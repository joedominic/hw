"""
Tests for Prompt Studio & Workbench:
- Template variable extraction and safe injection
- Audit version history CRUD and restore operations
- Prompt Studio AJAX endpoints (save, test-run, history, restore, delete, clear)
- Permissions (Staff only)
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from resume_app.models import (
    AtsJudgeProfile,
    JobListing,
    PromptVersionHistory,
    SystemPromptProfile,
    UserResume,
)
from resume_app.prompt_store import (
    clear_prompt_versions,
    delete_prompt_version,
    extract_template_variables,
    get_prompt_version,
    get_system_prompt_profile,
    inject_variables_into_template,
    list_prompt_versions,
    record_prompt_version,
    restore_prompt_version,
)

User = get_user_model()


class PromptStoreHelpersTestCase(TestCase):
    def test_extract_template_variables(self):
        text = "Hello {name}, your role is {target_role}. Please review {resume_text}."
        vars_found = extract_template_variables(text)
        self.assertEqual(vars_found, ["name", "target_role", "resume_text"])

    def test_extract_template_variables_ignores_json_braces(self):
        text = """
        Output JSON:
        {"candidate": "{resume_text}", "skills": ["python", "django"]}
        Double braces {{escaped_var}} should not match.
        """
        vars_found = extract_template_variables(text)
        self.assertIn("resume_text", vars_found)
        self.assertNotIn("escaped_var", vars_found)
        self.assertNotIn("candidate", vars_found)
        self.assertNotIn("skills", vars_found)

    def test_inject_variables_into_template_safe(self):
        template = "Analyze {resume_text} for {job_description}. Schema: {\"match\": true}."
        variables = {
            "resume_text": "Experienced Python Engineer",
            "job_description": "Senior Backend Dev",
        }
        result = inject_variables_into_template(template, variables)
        self.assertEqual(
            result,
            "Analyze Experienced Python Engineer for Senior Backend Dev. Schema: {\"match\": true}."
        )

    def test_inject_variables_missing_key_preserved(self):
        template = "Hello {name}, score: {score}"
        variables = {"name": "Alex"}
        result = inject_variables_into_template(template, variables)
        self.assertEqual(result, "Hello Alex, score: {score}")

    def test_prompt_version_history_crud_and_restore(self):
        user = User.objects.create_user(username="staff_editor", email="staff@example.com", is_staff=True)

        # Clear existing
        clear_prompt_versions("writer")

        # Record v1
        v1 = record_prompt_version(
            prompt_key="writer",
            system_prompt="System v1",
            user_prompt="User v1",
            author=user,
            change_summary="Initial v1",
        )
        self.assertEqual(v1.version_number, 1)

        # Record v2
        v2 = record_prompt_version(
            prompt_key="writer",
            system_prompt="System v2",
            user_prompt="User v2",
            author=user,
            change_summary="Update v2",
        )
        self.assertEqual(v2.version_number, 2)

        # List
        versions = list_prompt_versions("writer")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].version_number, 2)
        self.assertEqual(versions[1].version_number, 1)

        # Restore v1
        ok, msg, data = restore_prompt_version(v1.id, user=user)
        self.assertTrue(ok)
        self.assertEqual(data["system"], "System v1")
        self.assertEqual(data["user"], "User v1")

        # SystemPromptProfile should reflect v1
        prof = get_system_prompt_profile()
        self.assertEqual(prof.writer_system, "System v1")
        self.assertEqual(prof.writer_user, "User v1")

        # Restore created v3 tracking the rollback
        versions_after = list_prompt_versions("writer")
        self.assertEqual(len(versions_after), 3)
        self.assertEqual(versions_after[0].version_number, 3)

        # Delete single
        del_ok = delete_prompt_version(v2.id)
        self.assertTrue(del_ok)
        self.assertIsNone(get_prompt_version(v2.id))

        # Clear remaining
        count = clear_prompt_versions("writer")
        self.assertEqual(count, 2)
        self.assertEqual(len(list_prompt_versions("writer")), 0)


class PromptStudioEndpointsTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="staff_tester",
            email="staff@hireedge.test",
            password="testpassword123",
            is_staff=True,
        )
        self.normal_user = User.objects.create_user(
            username="normal_user",
            email="normal@hireedge.test",
            password="testpassword123",
            is_staff=False,
        )

    def test_prompt_library_view_requires_staff(self):
        url = reverse("prompt_library")

        # Anonymous -> redirect to login (302) or 403
        res = self.client.get(url)
        self.assertIn(res.status_code, [302, 403])

        # Normal user -> 403
        self.client.force_login(self.normal_user)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 403)

        # Staff user -> 200
        self.client.force_login(self.staff_user)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Prompt Studio")
        self.assertContains(res, "Test Playground")

    def test_prompt_studio_save_api(self):
        self.client.force_login(self.staff_user)
        url = reverse("prompt_studio_save_api")

        payload = {
            "prompt_key": "matching",
            "system_prompt": "You are a Matchmaker LLM.",
            "user_prompt": "Score {resume_text} against {job_description}.",
            "legacy_combined": "",
            "change_summary": "Tuned matching weights",
        }
        res = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(data["version_number"], 1)

        # Verify database updated
        prof = get_system_prompt_profile()
        self.assertEqual(prof.matching_system, "You are a Matchmaker LLM.")
        self.assertEqual(prof.matching_user, "Score {resume_text} against {job_description}.")

    @patch("resume_app.llm.invoke_llm_messages")
    def test_prompt_studio_test_run_api(self, mock_invoke):
        mock_response = MagicMock()
        mock_response.content = "Evaluation Match Score: 85%"
        mock_response.usage_metadata = {"input_tokens": 120, "output_tokens": 15}
        mock_invoke.return_value = mock_response

        self.client.force_login(self.staff_user)
        url = reverse("prompt_studio_test_run_api")

        payload = {
            "prompt_key": "matching",
            "system_prompt": "System evaluator.",
            "user_prompt": "Evaluate candidate: {resume_text}",
            "variables": {
                "resume_text": "Full stack engineer with 5 yrs experience",
            },
            "engine": "cloud",
        }
        res = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["output"], "Evaluation Match Score: 85%")
        self.assertEqual(data["tokens"]["input_tokens"], 120)
        self.assertEqual(data["resolved_prompt"]["user"], "Evaluate candidate: Full stack engineer with 5 yrs experience")

    def test_prompt_studio_history_and_restore_api(self):
        self.client.force_login(self.staff_user)

        # Save twice to create versions
        save_url = reverse("prompt_studio_save_api")
        self.client.post(save_url, data=json.dumps({
            "prompt_key": "insights",
            "system_prompt": "Insights Sys v1",
            "user_prompt": "User v1",
            "change_summary": "Ver 1"
        }), content_type="application/json")

        self.client.post(save_url, data=json.dumps({
            "prompt_key": "insights",
            "system_prompt": "Insights Sys v2",
            "user_prompt": "User v2",
            "change_summary": "Ver 2"
        }), content_type="application/json")

        # History list
        history_url = reverse("prompt_studio_history_api") + "?prompt_key=insights"
        res = self.client.get(history_url)
        self.assertEqual(res.status_code, 200)
        hist = res.json()
        self.assertEqual(hist["status"], "success")
        self.assertGreaterEqual(len(hist["versions"]), 2)
        v1_id = hist["versions"][-1]["id"]

        # Restore v1
        restore_url = reverse("prompt_studio_restore_api")
        res_rest = self.client.post(restore_url, data=json.dumps({"version_id": v1_id}), content_type="application/json")
        self.assertEqual(res_rest.status_code, 200)
        rest_data = res_rest.json()
        self.assertEqual(rest_data["status"], "success")
        self.assertEqual(rest_data["system_prompt"], "Insights Sys v1")

        # Delete version
        delete_url = reverse("prompt_studio_delete_version_api")
        res_del = self.client.post(delete_url, data=json.dumps({"version_id": v1_id}), content_type="application/json")
        self.assertEqual(res_del.status_code, 200)

        # Clear history
        clear_url = reverse("prompt_studio_clear_history_api")
        res_clr = self.client.post(clear_url, data=json.dumps({"prompt_key": "insights"}), content_type="application/json")
        self.assertEqual(res_clr.status_code, 200)
        self.assertGreaterEqual(res_clr.json()["deleted_count"], 1)

    def test_prompt_studio_set_model_preference_api(self):
        self.client.force_login(self.staff_user)
        url = reverse("prompt_studio_set_model_preference_api")

        # Set model preference to Frontier LLM
        payload = {
            "prompt_key": "writer",
            "model_choice": "frontier",
        }
        res = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["assigned_model"], "frontier")

        from resume_app.models import TenantPromptModelPreference
        pref = TenantPromptModelPreference.objects.for_user(self.staff_user).filter(query_kind="optimizer_writer").first()
        self.assertIsNotNone(pref)
        self.assertEqual(pref.target_tier, "frontier")

        # Set model preference to SLM
        res_slm = self.client.post(url, data=json.dumps({"prompt_key": "writer", "model_choice": "slm"}), content_type="application/json")
        self.assertEqual(res_slm.status_code, 200)
        pref_slm = TenantPromptModelPreference.objects.for_user(self.staff_user).filter(query_kind="optimizer_writer").first()
        self.assertEqual(pref_slm.target_tier, "slm")

    def test_prompt_studio_save_with_assigned_model(self):
        self.client.force_login(self.staff_user)
        url = reverse("prompt_studio_save_api")

        payload = {
            "prompt_key": "career_profiler",
            "system_prompt": "Career Profiler System",
            "user_prompt": "Profile {resume_text}",
            "assigned_model": "frontier",
        }
        res = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 200)

        from resume_app.models import TenantPromptModelPreference
        pref = TenantPromptModelPreference.objects.for_user(self.staff_user).filter(query_kind="career_profiler").first()
        self.assertIsNotNone(pref)
        self.assertEqual(pref.target_tier, "frontier")

    @patch("resume_app.llm.invoke_llm_messages")
    def test_prompt_studio_test_run_with_model_choice(self, mock_invoke):
        mock_response = MagicMock()
        mock_response.content = "Model specific test output"
        mock_response.usage_metadata = {"input_tokens": 50, "output_tokens": 20}
        mock_invoke.return_value = mock_response

        self.client.force_login(self.staff_user)
        url = reverse("prompt_studio_test_run_api")

        payload = {
            "prompt_key": "writer",
            "system_prompt": "Write test.",
            "user_prompt": "Write for candidate",
            "variables": {},
            "model_choice": "slm",
        }
        res = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["output"], "Model specific test output")
        mock_invoke.assert_called_once()
        _, kwargs = mock_invoke.call_args
        self.assertEqual(kwargs.get("max_model_tier"), "slm")

    def test_prompt_definitions_exclude_legacy_judges(self):
        from resume_app.prompt_store import PROMPT_DEFINITIONS, PROMPT_TO_QUERY_KIND_MAP
        keys = [p["key"] for p in PROMPT_DEFINITIONS]
        self.assertNotIn("ats_judge", keys, "ats_judge should not be exposed in Prompt Studio templates")
        self.assertNotIn("recruiter_judge", keys, "recruiter_judge should not be exposed in Prompt Studio templates")
        self.assertNotIn("job_authenticity", keys, "job_authenticity should not be exposed in Prompt Studio templates")
        self.assertNotIn("job_authenticity", PROMPT_TO_QUERY_KIND_MAP)
        self.assertIn("resume_judge", keys, "resume_judge should be exposed in Prompt Studio templates")
        self.assertIn("resume_judge", PROMPT_TO_QUERY_KIND_MAP)

