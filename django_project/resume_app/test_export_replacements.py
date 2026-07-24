"""Tests for durable export replacement token storage."""
from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from resume_app.models import AppAutomationSettings
from resume_app.test_utils import create_user, login_client


class ExportReplacementsPersistenceTests(TestCase):
    def setUp(self):
        self.user = create_user("repl_user")
        self.client = Client()
        login_client(self.client, self.user)

    def test_save_persists_to_automation_settings(self):
        url = reverse("settings")
        response = self.client.post(
            url,
            {
                "action": "save_export_replacements",
                "replacement_token_0": "{{NAME}}",
                "replacement_value_0": "Ada Lovelace",
                "replacement_token_1": "",
                "replacement_value_1": "",
                "replacement_token_2": "",
                "replacement_value_2": "",
                "replacement_token_3": "",
                "replacement_value_3": "",
                "replacement_token_4": "",
                "replacement_value_4": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_obj = AppAutomationSettings.get_for_user(self.user)
        self.assertEqual(settings_obj.export_replacements[0]["token"], "{{NAME}}")
        self.assertEqual(settings_obj.export_replacements[0]["value"], "Ada Lovelace")

    def test_settings_page_reads_from_db_not_only_session(self):
        AppAutomationSettings.get_for_user(self.user).set_export_replacements(
            [{"token": "{{CITY}}", "value": "Austin"}]
        )
        # Clear session so DB must be the source.
        session = self.client.session
        session.pop("export_replacements", None)
        session.save()

        response = self.client.get(reverse("settings") + "?tab=replacements")
        self.assertEqual(response.status_code, 200)
        entries = response.context["replacement_entries"]
        self.assertEqual(entries[0]["token"], "{{CITY}}")
        self.assertEqual(entries[0]["value"], "Austin")

    def test_session_tokens_migrate_into_db_on_settings_view(self):
        session = self.client.session
        session["export_replacements"] = [{"token": "{{OLD}}", "value": "legacy"}]
        session.save()

        response = self.client.get(reverse("settings") + "?tab=replacements")
        self.assertEqual(response.status_code, 200)
        settings_obj = AppAutomationSettings.get_for_user(self.user)
        self.assertEqual(settings_obj.export_replacements[0]["token"], "{{OLD}}")
        self.assertEqual(settings_obj.export_replacements[0]["value"], "legacy")
