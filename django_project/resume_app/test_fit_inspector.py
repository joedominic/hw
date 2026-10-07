from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from unittest.mock import patch

from resume_app.models import JobListing, JobListingAction, JobListingEmbedding, Track, SearchProfile

User = get_user_model()


class FitInspectorTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testinspector", email="inspector@example.com", password="password123"
        )
        self.client = Client()
        self.client.force_login(self.user)
        Track.ensure_baseline(self.user)

    def test_fit_inspector_get_page(self):
        resp = self.client.get(reverse("fit_inspector"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fit Inspector")
        self.assertContains(resp, "Job Fit Simulator")

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

        # 3. Preference vectors for SearchProfile must immediately reflect removal and not return cached stale job
        pv2 = get_preference_vectors(user=self.user, track="dir-plano")
        self.assertIsNone(pv2)

    @patch("resume_app.embeddings.embed_full", return_value=[0.1] * 384)
    def test_fit_inspector_get_with_job_id(self, mock_embed):
        job = JobListing.objects.create(
            title="Principal AI Architect",
            company_name="InnovateAI",
            description="Lead machine learning models and cloud architecture.",
            source="test",
            external_id="j_inspect_1",
        )
        resp = self.client.get(reverse("fit_inspector") + f"?job_id={job.id}&track=ic")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Principal AI Architect")
        self.assertContains(resp, "InnovateAI")
        self.assertContains(resp, f"Job #{job.id}")

