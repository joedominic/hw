"""Staff Phase A: experience mode UI, billing nav, impersonate landing."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from resume_app.experience import is_power_user, set_experience_mode
from resume_app.models import UserExperienceSettings
from resume_app.test_utils import TEST_PASSWORD, TENANT_TEST_MIDDLEWARE, login_client

User = get_user_model()


@override_settings(MIDDLEWARE=TENANT_TEST_MIDDLEWARE, DEFAULT_EXPERIENCE_MODE="normal")
class StaffExperienceModeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="staff_exp",
            password=TEST_PASSWORD,
            email="staff_exp@test.com",
            is_staff=True,
        )
        perm = Permission.objects.get(codename="can_impersonate_users")
        self.staff.user_permissions.add(perm)
        self.tenant = User.objects.create_user(
            username="tenant_exp",
            password=TEST_PASSWORD,
            email="tenant_exp@test.com",
        )

    def test_staff_settings_shows_simple_advanced_toggle(self):
        login_client(self.client, self.staff)
        resp = self.client.get(reverse("settings") + "?tab=account")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Experience mode")
        self.assertContains(resp, "Simple")
        self.assertContains(resp, "Advanced")

    def test_staff_can_switch_to_advanced(self):
        login_client(self.client, self.staff)
        resp = self.client.post(
            reverse("settings"),
            {"action": "save_experience_mode", "experience_mode": "power"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(is_power_user(self.staff))
        resp = self.client.get(reverse("career_cockpit"))
        self.assertContains(resp, "Advanced")

    def test_tenant_settings_hides_experience_toggle(self):
        login_client(self.client, self.tenant)
        resp = self.client.get(reverse("settings") + "?tab=account")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Experience mode")

    def test_staff_as_self_billing_nav_is_single_entry(self):
        login_client(self.client, self.staff)
        resp = self.client.get(reverse("settings"))
        self.assertContains(resp, "Billing")
        # Plan & usage is tenant / hijack-only; staff-as-self uses Admin Console → Billing
        self.assertNotContains(resp, "Plan &amp; usage")
        self.assertContains(resp, "Staff Console")

    def test_staff_without_impersonate_hides_staff_console(self):
        limited = User.objects.create_user(
            username="staff_no_imp",
            password=TEST_PASSWORD,
            is_staff=True,
        )
        login_client(self.client, limited)
        resp = self.client.get(reverse("settings"))
        self.assertNotContains(resp, "Staff Console")
        self.assertContains(resp, "Billing")

    def test_trial_days_form_does_not_share_assign_plan(self):
        import re

        login_client(self.client, self.staff)
        resp = self.client.get(reverse("staff_users"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        forms = re.findall(r"<form\b.*?</form>", html, flags=re.S)
        trial_forms = [form for form in forms if 'name="trial_days"' in form]
        self.assertGreaterEqual(len(trial_forms), 1)
        for form in trial_forms:
            self.assertIn('value="extend_trial"', form)
            self.assertNotIn("assign_plan", form)

    def test_cockpit_shows_hijack_banner(self):
        login_client(self.client, self.staff)
        self.client.post(
            "/hijack/acquire/",
            {"user_pk": self.tenant.pk, "next": reverse("career_cockpit")},
        )
        pages = (
            "career_cockpit",
            "performance_dashboard",
            "career_profiler",
            "settings",
            "billing",
            "privacy",
            "terms",
        )
        for name in pages:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertContains(resp, "Viewing as")
            self.assertContains(resp, self.tenant.username)
            self.assertContains(resp, "Release")
            self.assertNotContains(resp, "stop impersonating")
            self.assertNotContains(resp, "working on behalf")
            self.assertNotContains(resp, 'id="djhj"')

    def test_impersonate_form_defaults_to_cockpit(self):
        login_client(self.client, self.staff)
        resp = self.client.get(reverse("staff_users"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("career_cockpit"))
        self.assertNotContains(resp, reverse("resume_optimizer"))

    def test_experience_mode_during_hijack_updates_staff_not_tenant(self):
        login_client(self.client, self.staff)
        self.client.post(
            "/hijack/acquire/",
            {"user_pk": self.tenant.pk, "next": reverse("career_cockpit")},
        )
        resp = self.client.post(
            reverse("settings"),
            {"action": "save_experience_mode", "experience_mode": "power"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(is_power_user(self.staff))
        self.assertFalse(is_power_user(self.tenant))
        exp_tenant = UserExperienceSettings.get_for_user(self.tenant)
        self.assertEqual(exp_tenant.experience_mode, UserExperienceSettings.ExperienceMode.NORMAL)

    def test_hijack_settings_app_tab_redirects_to_cockpit_automation(self):
        login_client(self.client, self.staff)
        self.client.post(
            "/hijack/acquire/",
            {"user_pk": self.tenant.pk, "next": reverse("career_cockpit")},
        )
        resp = self.client.get(reverse("settings") + "?tab=app")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("drawer=automation", resp["Location"])

    def test_huey_readable_while_hijacked_mutations_blocked(self):
        login_client(self.client, self.staff)
        self.client.post(
            "/hijack/acquire/",
            {"user_pk": self.tenant.pk, "next": reverse("career_cockpit")},
        )
        resp = self.client.get(reverse("huey_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["huey_readonly"])
        self.assertContains(resp, "Read-only while impersonating")

        mutate = self.client.post(reverse("huey_run_cleanup_now"))
        self.assertEqual(mutate.status_code, 302)
        self.assertEqual(mutate["Location"], reverse("huey_dashboard"))

    def test_huey_owner_and_failed_filters(self):
        from resume_app.models import JobSearchTask, JobSearchTaskRun

        task = JobSearchTask.objects.create(
            owner=self.tenant,
            name="Tenant Scrape",
            search_term="django",
            frequency="0 9 * * *",
        )
        JobSearchTaskRun.objects.create(
            task=task,
            status=JobSearchTaskRun.STATUS_FAILED,
            error_message="boom",
        )
        login_client(self.client, self.staff)
        resp = self.client.get(
            reverse("huey_dashboard") + f"?owner={self.tenant.username}&failed=1"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Tenant Scrape")
        self.assertContains(resp, self.tenant.username)

    def test_staff_account_defaults_tab_label(self):
        login_client(self.client, self.staff)
        resp = self.client.get(reverse("settings") + "?tab=app")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Account automation defaults")
        self.assertContains(resp, "your staff account")

    def test_optimizer_save_draft_ajax_does_not_reload(self):
        from resume_app.models import JobDescription, JobListing, OptimizedResume, PipelineEntry, UserResume

        ur = UserResume.objects.create(owner=self.tenant, file="test.pdf", original_filename="t.pdf")
        jd = JobDescription.objects.create(content="JD content")
        jl = JobListing.objects.create(title="Eng Lead", company_name="Co", source="test", external_id="opt-draft-test")
        opt = OptimizedResume.objects.create(
            owner=self.tenant,
            original_resume=ur,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="Initial content",
        )
        login_client(self.client, self.tenant)
        resp = self.client.post(
            reverse("resume_optimizer"),
            {
                "action": "save_draft",
                "markdown": "Updated draft without reload",
                "opt_id": opt.id,
                "job_id": jl.id,
                "track": "default",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("Draft resume saved", data.get("message", ""))

        opt.refresh_from_db()
        self.assertEqual(opt.optimized_content, "Updated draft without reload")

        pe = PipelineEntry.objects.filter(owner=self.tenant, job_listing=jl).first()
        self.assertIsNotNone(pe)
        self.assertEqual(pe.applied_resume_markdown, "Updated draft without reload")

    def test_optimizer_template_button_hierarchy(self):
        login_client(self.client, self.tenant)
        resp = self.client.get(reverse("resume_optimizer"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="save-draft-top-btn"')
        content = resp.content.decode("utf-8")
        self.assertNotIn('html += \'<div class="flex items-center gap-2"><button type="button" id="save-draft-btn"', content)
        self.assertIn('html += \'<button type="button" id="save-draft-btn"', content)
        self.assertIn("window.submitSaveDraft", content)
