from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from unittest.mock import patch

from resume_app.models import JobListing, JobListingAction, JobListingEmbedding, Track, SearchProfile

User = get_user_model()


class FitInspectorTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testinspector",
            email="inspector@example.com",
            password="password123",
            is_staff=True,
        )
        self.client = Client()
        self.client.force_login(self.user)
        Track.ensure_baseline(self.user)

    def test_fit_inspector_get_page(self):
        resp = self.client.get(reverse("fit_inspector"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fit Inspector")
        self.assertContains(resp, "Job Fit Simulator")

    def test_tenant_redirected_to_cockpit(self):
        tenant = User.objects.create_user(username="tenant_fit", password="password123")
        client = Client()
        client.force_login(tenant)
        resp = client.get(reverse("fit_inspector"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/jobs/cockpit/", resp["Location"])

    @patch("resume_app.embeddings.embed_full", return_value=[0.1] * 384)
    @patch("resume_app.preference.get_preference_vectors")
    @patch("resume_app.preference.get_disliked_embeddings")
    def test_fit_inspector_analyze_action(self, mock_disliked, mock_prefs, mock_embed):
        liked_vec = [0.1] * 384
        disliked_vec = [-0.1] * 384
        liked_jobs = [(101, "Senior Python Engineer", "Acme", liked_vec, None)]
        mock_prefs.return_value = (liked_vec, disliked_vec, liked_jobs)
        mock_disliked.return_value = [(202, disliked_vec)]

        JobListing.objects.create(id=202, title="Support Tech", company_name="BadCorp", source="test", external_id="d-1")

        resp = self.client.post(
            reverse("fit_inspector"),
            {
                "action": "analyze",
                "track": "ic",
                "title": "Lead Software Engineer",
                "company": "GoodCorp",
                "description": "Build high-throughput APIs in Python and Django.",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Liked Similarity")
        self.assertContains(resp, "Preference Margin")
        self.assertContains(resp, "Senior Python Engineer")

    def test_fit_inspector_remove_like_and_dislike(self):
        job1 = JobListing.objects.create(title="Job 1", company_name="C1", source="test", external_id="j1")
        job2 = JobListing.objects.create(title="Job 2", company_name="C2", source="test", external_id="j2")

        JobListingAction.objects.create(owner=self.user, job_listing=job1, action=JobListingAction.ActionType.LIKED, track="ic")
        JobListingEmbedding.objects.create(owner=self.user, job_listing=job1, embedding_type=JobListingEmbedding.EmbeddingType.LIKED, track="ic", embedding=[0.1]*384)

        JobListingAction.objects.create(owner=self.user, job_listing=job2, action=JobListingAction.ActionType.DISLIKED, track="ic")
        JobListingEmbedding.objects.create(owner=self.user, job_listing=job2, embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED, track="ic", embedding=[0.1]*384)

        # Remove like
        resp = self.client.post(reverse("fit_inspector"), {"action": "remove_like", "job_id": job1.id, "track": "ic"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(JobListingAction.objects.filter(owner=self.user, job_listing=job1, action=JobListingAction.ActionType.LIKED).exists())

        # Remove dislike
        resp = self.client.post(reverse("fit_inspector"), {"action": "remove_dislike", "job_id": job2.id, "track": "ic"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(JobListingAction.objects.filter(owner=self.user, job_listing=job2, action=JobListingAction.ActionType.DISLIKED).exists())

    def test_fit_inspector_search_profiles_and_liked_default(self):
        SearchProfile.objects.create(owner=self.user, name="Dir-Plano", slug="dir-plano")
        SearchProfile.objects.create(owner=self.user, name="SP-Eng_Plano", slug="sp-eng_plano")

        job1 = JobListing.objects.create(title="Liked Leader", company_name="GoodCorp", source="test", external_id="j1")
        job2 = JobListing.objects.create(title="Disliked Support", company_name="BadCorp", source="test", external_id="j2")

        JobListingAction.objects.create(owner=self.user, job_listing=job1, action=JobListingAction.ActionType.LIKED, track="dir-plano")
        JobListingAction.objects.create(owner=self.user, job_listing=job2, action=JobListingAction.ActionType.DISLIKED, track="dir-plano")

        resp = self.client.get(reverse("fit_inspector") + "?track=dir-plano")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Dir-Plano")
        self.assertContains(resp, "SP-Eng_Plano")
        self.assertEqual(resp.context["default_filter"], "liked")
        self.assertEqual(resp.context["liked_count"], 1)
        self.assertEqual(resp.context["disliked_count"], 1)
        self.assertContains(resp, "Liked Jobs (1)")
        self.assertContains(resp, "Disliked Jobs (1)")

    @patch("resume_app.embeddings.embed_full", return_value=[0.1] * 384)
    def test_removed_job_immediately_purged_from_search_profile_cache(self, mock_embed):
        from resume_app.preference import get_preference_vectors

        SearchProfile.objects.create(owner=self.user, name="Dir-Plano", slug="dir-plano")
        job1 = JobListing.objects.create(title="Legacy Lead", company_name="CorpA", source="test", external_id="j_rem1")
        JobListingAction.objects.create(owner=self.user, job_listing=job1, action=JobListingAction.ActionType.LIKED, track="dir-plano")
        JobListingEmbedding.objects.create(owner=self.user, job_listing=job1, embedding_type=JobListingEmbedding.EmbeddingType.LIKED, track="dir-plano", embedding=[0.1]*384)

        # 1. Warm cache for search profile
        pv1 = get_preference_vectors(user=self.user, track="dir-plano")
        self.assertIsNotNone(pv1)
        self.assertEqual(len(pv1[2]), 1)
        self.assertEqual(pv1[2][0][0], job1.id)

        # 2. Remove like via Fit Inspector POST
        resp = self.client.post(
            reverse("fit_inspector"),
            {
                "action": "remove_like",
                "job_id": job1.id,
                "track": "dir-plano",
                "title": "Lead Software Engineer",
                "description": "Some requirements",
            },
        )
        self.assertEqual(resp.status_code, 200)

        # 3. Cache must reflect removal
        pv2 = get_preference_vectors(user=self.user, track="dir-plano")
        liked_ids = [j[0] for j in (pv2[2] if pv2 else [])]
        self.assertNotIn(job1.id, liked_ids)

    @patch("resume_app.embeddings.embed_full", return_value=[0.1] * 384)
    def test_fit_inspector_get_with_job_id(self, mock_embed):
        job = JobListing.objects.create(
            title="Inspect Me",
            company_name="Acme",
            description="Python Django role",
            source="test",
            external_id="inspect-1",
        )
        resp = self.client.get(reverse("fit_inspector") + f"?job_id={job.id}&track=ic")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Inspect Me")


class FitInspectorHijackTests(TestCase):
    """Support can diagnose tenant fit data while impersonating."""

    def setUp(self):
        self.support = User.objects.create_user(
            username="fit_support",
            password="pass12345!",
            is_staff=True,
        )
        perm = Permission.objects.get(codename="can_impersonate_users")
        self.support.user_permissions.add(perm)
        self.target = User.objects.create_user(username="fit_target", password="pass12345!")
        Track.ensure_baseline(self.target)
        SearchProfile.objects.create(owner=self.target, name="Target Profile", slug="target-profile")
        self.job = JobListing.objects.create(
            title="Target Liked Role",
            company_name="GoodCo",
            source="test",
            external_id="fit-hijack-1",
        )
        JobListingAction.objects.create(
            owner=self.target,
            job_listing=self.job,
            action=JobListingAction.ActionType.LIKED,
            track="target-profile",
        )

    def test_fit_inspector_while_hijacked_shows_tenant_data(self):
        client = Client()
        client.login(username="fit_support", password="pass12345!")
        resp = client.post(
            "/hijack/acquire/",
            {"user_pk": self.target.pk, "next": reverse("career_cockpit")},
        )
        self.assertEqual(resp.status_code, 302)

        resp = client.get(reverse("fit_inspector") + "?track=target-profile")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Target Profile")
        self.assertContains(resp, "Target Liked Role")
        self.assertEqual(resp.context["liked_count"], 1)
