"""Cross-user isolation, ownership enforcement, and impersonation tests."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase

from resume_app.models import (
    ApplicantProfile,
    AppAutomationSettings,
    AtsJudgeProfile,
    JobDescription,
    JobListing,
    JobSearchTask,
    JobSearchTaskRun,
    LLMProviderConfig,
    OptimizedResume,
    PipelineEntry,
    Track,
    UserResume,
)

User = get_user_model()


class MultiTenantIsolationTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(username="alice", password="pass12345!")
        self.user_b = User.objects.create_user(username="bob", password="pass12345!")
        Track.ensure_baseline(self.user_a)
        Track.ensure_baseline(self.user_b)

    # ------------------------------------------------------------------
    # Existing: OptimizedResume isolation via Ninja API
    # ------------------------------------------------------------------
    def test_user_a_cannot_see_user_b_optimized_resume_via_api(self):
        """GET /api/resume/status/<id> must 404 when the OptimizedResume belongs to another user."""
        resume_b = UserResume.objects.create(
            owner=self.user_b,
            original_filename="bob.pdf",
            is_library=True,
        )
        jd = JobDescription.objects.create(content="some job")
        opt_b = OptimizedResume.objects.create(
            owner=self.user_b,
            original_resume=resume_b,
            job_description=jd,
        )
        client = Client()
        client.login(username="alice", password="pass12345!")
        # The API endpoint is keyed on OptimizedResume.pk, not UserResume.pk
        resp = client.get(f"/api/resume/status/{opt_b.id}")
        self.assertIn(resp.status_code, (403, 404))

    def test_pipeline_entries_scoped_per_user(self):
        job = JobListing.objects.create(
            source="test",
            external_id="1",
            title="Engineer",
            company_name="Co",
        )
        PipelineEntry.objects.create(
            owner=self.user_a,
            job_listing=job,
            track="ic",
            stage="pipeline",
        )
        self.assertEqual(PipelineEntry.objects.for_user(self.user_a).count(), 1)
        self.assertEqual(PipelineEntry.objects.for_user(self.user_b).count(), 0)

    def test_signup_seeds_profile_rows(self):
        user = User.objects.create_user(username="carol", password="pass12345!")
        Track.ensure_baseline(user)
        self.assertTrue(ApplicantProfile.objects.filter(owner=user).exists())
        self.assertTrue(AppAutomationSettings.objects.filter(owner=user).exists())
        self.assertGreaterEqual(Track.objects.for_user(user).count(), 1)

    # ------------------------------------------------------------------
    # LLM provider isolation
    # ------------------------------------------------------------------
    def test_llm_provider_config_scoped_per_user(self):
        """LLMProviderConfig.objects.for_user() must not return another user's configs."""
        LLMProviderConfig.objects.create(
            owner=self.user_b,
            provider="OpenAI",
            encrypted_api_key="enc-bob",
        )
        self.assertEqual(LLMProviderConfig.objects.for_user(self.user_a).count(), 0)
        self.assertEqual(LLMProviderConfig.objects.for_user(self.user_b).count(), 1)

    def test_llm_session_active_provider_scoped(self):
        """get_active_llm_provider(user) must return per-user active config only."""
        from resume_app.llm.session import get_active_llm_provider

        LLMProviderConfig.objects.create(
            owner=self.user_b,
            provider="Anthropic",
            encrypted_api_key="enc-bob-anthropic",
            is_active=True,
        )
        # user_a has no configs — must return None, not bob's active provider
        result_a = get_active_llm_provider(self.user_a)
        self.assertIsNone(result_a)

        result_b = get_active_llm_provider(self.user_b)
        self.assertEqual(result_b, "Anthropic")

    # ------------------------------------------------------------------
    # Draft-save IDOR
    # ------------------------------------------------------------------
    def test_draft_save_rejects_wrong_owner(self):
        """save_optimized_draft_content(user=alice) must 404 on bob's OptimizedResume."""
        from resume_app.services import DraftSaveError, save_optimized_draft_content

        resume_b = UserResume.objects.create(
            owner=self.user_b,
            original_filename="bob.pdf",
            is_library=True,
        )
        jd = JobDescription.objects.create(content="a job")
        opt_b = OptimizedResume.objects.create(
            owner=self.user_b,
            original_resume=resume_b,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="original content",
        )
        with self.assertRaises(DraftSaveError) as ctx:
            save_optimized_draft_content(opt_b.id, "hacked content", user=self.user_a)
        self.assertEqual(ctx.exception.status_code, 404)
        opt_b.refresh_from_db()
        self.assertEqual(opt_b.optimized_content, "original content")

    def test_draft_save_accepts_correct_owner(self):
        """save_optimized_draft_content(user=alice) must succeed on alice's OptimizedResume."""
        from resume_app.services import save_optimized_draft_content

        resume_a = UserResume.objects.create(
            owner=self.user_a,
            original_filename="alice.pdf",
            is_library=True,
        )
        jd = JobDescription.objects.create(content="a job")
        opt_a = OptimizedResume.objects.create(
            owner=self.user_a,
            original_resume=resume_a,
            job_description=jd,
            status=OptimizedResume.STATUS_COMPLETED,
            optimized_content="original",
        )
        save_optimized_draft_content(opt_a.id, "updated content", user=self.user_a)
        opt_a.refresh_from_db()
        self.assertEqual(opt_a.optimized_content, "updated content")

    # ------------------------------------------------------------------
    # User-scoped dedupe
    # ------------------------------------------------------------------
    def test_dedupe_only_removes_current_user_entries(self):
        """dedupe_pipeline_entries must never touch another tenant's rows."""
        from resume_app.job_dedupe import dedupe_pipeline_entries

        job_a1 = JobListing.objects.create(
            source="test",
            external_id="da1",
            title="Engineer",
            company_name="Acme",
            description="x" * 100,
        )
        job_a2 = JobListing.objects.create(
            source="test",
            external_id="da2",
            title="Engineer",
            company_name="Acme",
            description="x" * 100,  # same fingerprint as job_a1
        )
        # Both users have pipeline entries for these jobs
        pe_a1 = PipelineEntry.objects.create(
            owner=self.user_a, job_listing=job_a1, track="ic", stage="pipeline"
        )
        pe_a2 = PipelineEntry.objects.create(
            owner=self.user_a, job_listing=job_a2, track="ic", stage="pipeline"
        )
        pe_b = PipelineEntry.objects.create(
            owner=self.user_b, job_listing=job_a1, track="ic", stage="pipeline"
        )

        result = dedupe_pipeline_entries(
            user=self.user_a,
            track_slug="ic",
            stage="pipeline",
            include_done=False,
        )
        self.assertGreaterEqual(result["entries_removed"], 1)

        # Bob's entry must be untouched
        pe_b.refresh_from_db()
        self.assertIsNone(pe_b.removed_at)


class ImpersonationTests(TestCase):
    def setUp(self):
        self.support = User.objects.create_user(
            username="support",
            password="pass12345!",
            is_staff=True,
        )
        perm = Permission.objects.get(codename="can_impersonate_users")
        self.support.user_permissions.add(perm)
        self.target = User.objects.create_user(username="target", password="pass12345!")
        Track.ensure_baseline(self.target)
        UserResume.objects.create(
            owner=self.target,
            original_filename="target.pdf",
            is_library=True,
        )

    def test_support_can_hijack_and_see_target_data(self):
        client = Client()
        client.login(username="support", password="pass12345!")
        resp = client.post(
            "/hijack/acquire/",
            {"user_pk": self.target.pk, "next": "/"},
        )
        self.assertEqual(resp.status_code, 302)
        resp = client.get("/api/resume/jobs/resumes")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)

    def test_regular_user_cannot_access_staff_users(self):
        client = Client()
        client.login(username="target", password="pass12345!")
        resp = client.get("/staff/users/")
        self.assertEqual(resp.status_code, 403)

    def test_staff_menu_hides_jobs_unless_impersonating(self):
        client = Client()
        client.login(username="support", password="pass12345!")

        # Staff user browsing settings: JOBS section should NOT be present
        resp = client.get("/settings/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("Admin Console", content)
        self.assertNotIn(">Find jobs<", content)
        self.assertNotIn(">My jobs<", content)

        # Hijack target candidate: JOBS section MUST be present
        client.post("/hijack/acquire/", {"user_pk": self.target.pk, "next": "/"})
        resp = client.get("/settings/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("Viewing as", content)
        self.assertIn("Find jobs", content)
        self.assertIn("My jobs", content)


class MonitorAccessControlTests(TestCase):
    """Huey monitor views must be staff-only."""

    def setUp(self):
        self.user = User.objects.create_user(username="regular", password="pass12345!")
        self.staff = User.objects.create_user(
            username="admin", password="pass12345!", is_staff=True
        )
        Track.ensure_baseline(self.user)
        Track.ensure_baseline(self.staff)

    def test_regular_user_cannot_access_monitor(self):
        client = Client()
        client.login(username="regular", password="pass12345!")
        resp = client.get("/jobs/huey/")
        self.assertIn(resp.status_code, (403, 302))

    def test_staff_user_can_access_monitor(self):
        client = Client()
        client.login(username="admin", password="pass12345!")
        resp = client.get("/jobs/huey/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Worker &amp; Task Queue Monitor")
        self.assertContains(resp, "Periodic Task Schedulers")
        self.assertContains(resp, "Queue Backlog")

    def test_staff_monitor_renders_task_runs(self):
        task = JobSearchTask.objects.create(
            owner=self.user,
            name="Senior Backend Role",
            search_term="Python Django",
            frequency="0 * * * *",
        )
        JobSearchTaskRun.objects.create(
            task=task,
            status=JobSearchTaskRun.STATUS_COMPLETED,
            jobs_fetched=12,
            jobs_added_to_pipeline=3,
        )

        client = Client()
        client.login(username="admin", password="pass12345!")
        resp = client.get("/jobs/huey/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Senior Backend Role")
        self.assertContains(resp, "12")


class FocusBreakdownStaffOnlyTests(TestCase):
    """Focus-score breakdown pages/API expose ranking internals — staff only."""

    def setUp(self):
        self.user = User.objects.create_user(username="regular_fit", password="pass12345!")
        self.staff = User.objects.create_user(
            username="admin_fit", password="pass12345!", is_staff=True
        )
        Track.ensure_baseline(self.user)
        Track.ensure_baseline(self.staff)
        self.job = JobListing.objects.create(
            title="Staff Gate Test Role",
            company_name="Acme",
            description="Build things.",
            source="test",
            external_id="focus-staff-gate-1",
        )

    def test_regular_user_cannot_open_focus_breakdown_page(self):
        client = Client()
        client.login(username="regular_fit", password="pass12345!")
        resp = client.get(f"/jobs/{self.job.id}/focus-breakdown/")
        self.assertEqual(resp.status_code, 403)

    def test_regular_user_cannot_call_focus_breakdown_api(self):
        client = Client()
        client.login(username="regular_fit", password="pass12345!")
        resp = client.get(f"/api/resume/jobs/focus-breakdown/{self.job.id}")
        self.assertEqual(resp.status_code, 403)

    def test_staff_user_is_not_forbidden_from_focus_breakdown_page(self):
        client = Client()
        client.login(username="admin_fit", password="pass12345!")
        resp = client.get(f"/jobs/{self.job.id}/focus-breakdown/")
        self.assertNotEqual(resp.status_code, 403)


class OptimizerAtsProfileScopingTests(TestCase):
    """Optimizer lists global admin-managed ATS judge profiles and falls back to default."""

    def setUp(self):
        self.user_a = User.objects.create_user(username="alice_opt", password="pass12345!")
        self.user_b = User.objects.create_user(username="bob_opt", password="pass12345!")
        self.global_default = AtsJudgeProfile.objects.filter(owner__isnull=True, is_default=True).first()
        if self.global_default is None:
            self.global_default = AtsJudgeProfile.objects.create(
                owner=None,
                name="Global Default",
                slug="global-default",
                is_default=True,
                ats_judge_system="DEFAULT_SYS",
                ats_judge_user="DEFAULT_USR",
            )
        self.global_custom = AtsJudgeProfile.objects.create(
            owner=None,
            name="Global Custom",
            slug="global-custom",
            is_default=False,
            ats_judge_system="CUSTOM_SYS",
            ats_judge_user="CUSTOM_USR",
        )

    def test_optimizer_page_lists_global_ats_profiles(self):
        client = Client()
        client.login(username="alice_opt", password="pass12345!")
        resp = client.get("/resume/optimizer/")
        self.assertEqual(resp.status_code, 200)
        profiles = resp.context["ats_profiles"]
        ids = {p.pk for p in profiles}
        self.assertIn(self.global_default.pk, ids)
        self.assertIn(self.global_custom.pk, ids)
        self.assertEqual(resp.context["selected_ats_profile_id"], self.global_default.pk)

    def test_optimizer_ignores_invalid_session_ats_id(self):
        client = Client()
        client.login(username="alice_opt", password="pass12345!")
        session = client.session
        session["optimizer_ats_judge_profile_id"] = 999999
        session.save()
        resp = client.get("/resume/optimizer/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["selected_ats_profile_id"], self.global_default.pk)


class LandingPageTests(TestCase):
    def test_anonymous_user_sees_landing(self):
        resp = Client().get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Your job search, smarter from day one")
        self.assertContains(resp, "HireEdge")
        self.assertContains(resp, 'href="/accounts/login/"')
        self.assertContains(resp, 'href="/accounts/signup/"')

    def test_authenticated_user_redirects_to_getting_started(self):
        user = User.objects.create_user(username="landing", password="pass12345!")
        client = Client()
        client.login(username=user.username, password="pass12345!")
        resp = client.get("/")
        self.assertRedirects(resp, "/getting-started/", fetch_redirect_response=False)


class LegalPagesTests(TestCase):
    def test_privacy_and_terms_are_public(self):
        client = Client()
        self.assertEqual(client.get("/legal/privacy/").status_code, 200)
        self.assertEqual(client.get("/legal/terms/").status_code, 200)
        self.assertContains(client.get("/legal/privacy/"), "Privacy Policy")
        self.assertContains(client.get("/legal/terms/"), "Terms of Service")


class SetStaffUserCommandTests(TestCase):
    def test_grant_and_revoke_staff_and_support(self):
        from django.core.management import call_command

        user = User.objects.create_user(username="ops", password="pass12345!")
        self.assertFalse(user.is_staff)

        call_command("set_staff_user", "ops")
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertTrue(user.groups.filter(name="Support").exists())
        self.assertTrue(user.has_perm("resume_app.can_impersonate_users"))

        call_command("set_staff_user", "ops", "--remove")
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.groups.filter(name="Support").exists())
