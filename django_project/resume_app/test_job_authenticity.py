from datetime import datetime, timezone as dt_tz
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.utils import timezone
from django.contrib.auth import get_user_model
from resume_app.models import JobListing, SearchProfile
from resume_app.authenticity import JobAuthenticityService

User = get_user_model()


class JobAuthenticityUnitTests(TestCase):
    """
    Unit tests for deterministic labor compliance and job authenticity scoring rules.
    """

    def test_calculate_score_and_verdict_perm_compliance(self):
        """If perm_legal_phrasing is detected, verdict must be PERM_COMPLIANCE."""
        audit_trail = [
            {"check": "perm_legal_phrasing", "evidence_quote": "post-baccalaureate progressive experience", "detected": True, "points": 40},
            {"check": "rigid_degree_equation", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "flat_salary_range", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "statutory_minimum_window_clause", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "hyper_specific_okrs", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "title_scope_mismatch", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "flexible_general_req", "evidence_quote": "NONE", "detected": False, "points": 0},
        ]
        score, verdict, expression, confidence = JobAuthenticityService.calculate_score_and_verdict(audit_trail)
        self.assertEqual(score, 40)
        self.assertEqual(verdict, "PERM_COMPLIANCE")
        self.assertEqual(confidence, "HIGH")
        self.assertIn("40", expression)

    def test_calculate_score_and_verdict_internal_preselect(self):
        """If score >= 40 and not PERM, verdict must be INTERNAL_PRESELECT."""
        audit_trail = [
            {"check": "perm_legal_phrasing", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "phantom_pipeline_or_talent_pool", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "short_lifespan_or_compliance_window", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "generic_jfa_template_only", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "senior_promotion_or_scope_mismatch", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "bespoke_contractor_conversion_stack", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "hyper_specific_okrs", "evidence_quote": "Project Aegis $6.1B run-rate", "detected": True, "points": 20},
            {"check": "rigid_degree_equation", "evidence_quote": "Master's + 3 years or Bachelor's + 5 years strictly", "detected": True, "points": 20},
            {"check": "flat_salary_range", "evidence_quote": "$180,000 - $185,000", "detected": True, "points": 15},
            {"check": "flexible_general_req", "evidence_quote": "NONE", "detected": False, "points": 0},
        ]
        # 0 + 0 + 0 + 0 + 0 + 0 + 20 + 20 + 15 - 0 = 55
        score, verdict, expression, confidence = JobAuthenticityService.calculate_score_and_verdict(audit_trail)
        self.assertEqual(score, 55)
        self.assertEqual(verdict, "INTERNAL_PRESELECT")
        self.assertEqual(confidence, "HIGH")

    def test_calculate_score_and_verdict_phantom_pipeline(self):
        """If phantom_pipeline_or_talent_pool detected, verdict must be PHANTOM_PIPELINE."""
        audit_trail = [
            {"check": "perm_legal_phrasing", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "phantom_pipeline_or_talent_pool", "evidence_quote": "join our talent community for future openings", "detected": True, "points": 50},
            {"check": "short_lifespan_or_compliance_window", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "generic_jfa_template_only", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "senior_promotion_or_scope_mismatch", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "bespoke_contractor_conversion_stack", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "hyper_specific_okrs", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "rigid_degree_equation", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "flat_salary_range", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "flexible_general_req", "evidence_quote": "NONE", "detected": False, "points": 0},
        ]
        score, verdict, expression, confidence = JobAuthenticityService.calculate_score_and_verdict(audit_trail)
        self.assertEqual(verdict, "PHANTOM_PIPELINE")
        self.assertEqual(score, 50)

    def test_calculate_score_and_verdict_genuine_external_with_clamping(self):
        """Flexible requirements reduce score by 20; score clamped to [0, 100]."""
        audit_trail = [
            {"check": "perm_legal_phrasing", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "phantom_pipeline_or_talent_pool", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "short_lifespan_or_compliance_window", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "generic_jfa_template_only", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "senior_promotion_or_scope_mismatch", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "bespoke_contractor_conversion_stack", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "hyper_specific_okrs", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "rigid_degree_equation", "evidence_quote": "NONE", "detected": False, "points": 0},
            {"check": "flat_salary_range", "evidence_quote": "$180,000 - $185,000", "detected": True, "points": 15},
            {"check": "flexible_general_req", "evidence_quote": "Java, Python, or Go", "detected": True, "points": -20},
        ]
        # 15 - 20 = -5 -> clamped to 0
        score, verdict, expression, confidence = JobAuthenticityService.calculate_score_and_verdict(audit_trail)
        self.assertEqual(score, 0)
        self.assertEqual(verdict, "GENUINE_EXTERNAL")
        self.assertEqual(confidence, "HIGH")

    def test_fallback_heuristic_perm(self):
        """Regex heuristic flags statutory DOL phrases when LLM is unavailable."""
        jd = "Candidates must possess post-baccalaureate progressive experience or foreign educational equivalent."
        result = JobAuthenticityService._fallback_heuristic(jd)
        self.assertEqual(result["final_verdict"], "PERM_COMPLIANCE")
        self.assertGreaterEqual(result["scoring_calculation"]["calculated_score"], 40)

    def test_fallback_heuristic_genuine_external(self):
        """Open market JD with broad skills evaluates to GENUINE_EXTERNAL."""
        jd = "We are seeking a Software Engineer. Experience with Java, Python, or Go and equivalent experience welcome."
        result = JobAuthenticityService._fallback_heuristic(jd)
        self.assertEqual(result["final_verdict"], "GENUINE_EXTERNAL")
        self.assertEqual(result["scoring_calculation"]["calculated_score"], 0)

    def test_extract_window_and_deadline_short_lifespan(self):
        """Detect short lifespan when posting to deadline delta is <= 4 days."""
        job = JobListing(
            title="Systems Engineer",
            posted_at=datetime(2026, 10, 7, tzinfo=dt_tz.utc),
            description="The application window for this opening will close on - 9 Oct 2026.",
        )
        posted_str, win_str, is_short, quote, delta = JobAuthenticityService.extract_window_and_deadline(job)
        self.assertEqual(posted_str, "2026-10-07")
        self.assertEqual(delta, 2)
        self.assertTrue(is_short)
        self.assertIn("2 calendar days", win_str)

    def test_extract_window_and_deadline_statutory_minimum(self):
        """Detect statutory minimum business days window clause."""
        job = JobListing(
            title="Software Director",
            posted_at=datetime(2026, 10, 8, tzinfo=dt_tz.utc),
            description="This role is expected to accept applications for a minimum of 5 business days.",
        )
        posted_str, win_str, is_short, quote, delta = JobAuthenticityService.extract_window_and_deadline(job)
        self.assertTrue(is_short)
        self.assertIn("minimum of 5 business days", quote)
        self.assertIn("Statutory minimum", win_str)

    def test_fallback_heuristic_senior_role_short_window_preselect(self):
        """A senior/principal role with ultra-short window triggers Rule 3 and Rule 5 -> INTERNAL_PRESELECT."""
        job = JobListing(
            title="Principal Cloud Architect",
            posted_at=datetime(2026, 10, 7, tzinfo=dt_tz.utc),
            description="Seeking Principal Architect. Applications will close on 10/09/2026.",
        )
        result = JobAuthenticityService._fallback_heuristic(job)
        self.assertEqual(result["final_verdict"], "INTERNAL_PRESELECT")
        self.assertEqual(result["scoring_calculation"]["calculated_score"], 60)


class JobAuthenticityIntegrationTests(TestCase):
    """
    Integration tests for JobAuthenticityService persistence, caching, and REST endpoints.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="password123")
        self.client = Client()
        self.client.login(username="testuser", password="password123")

        self.job = JobListing.objects.create(
            title="Senior Staff Infrastructure Engineer",
            company_name="Enterprise Corp",
            location="Remote",
            external_id="ext-test-1",
            description="Seeking engineer with post-baccalaureate progressive experience in cloud migration.",
            source="manual",
        )

    def test_audit_persists_to_job_listing(self):
        """JobAuthenticityService.audit persists fields to the JobListing instance."""
        res = JobAuthenticityService.audit(self.job, use_llm=False)
        self.job.refresh_from_db()

        self.assertEqual(self.job.authenticity_verdict, "PERM_COMPLIANCE")
        self.assertEqual(self.job.authenticity_score, 40)
        self.assertIsNotNone(self.job.authenticity_audited_at)
        self.assertIn("DOL ETA-9089", self.job.authenticity_rationale)
        self.assertTrue(len(self.job.authenticity_audit_trail) >= 7)

    def test_get_authenticity_api_endpoint(self):
        """GET /api/resume/jobs/{id}/authenticity returns current audit payload."""
        # First audit
        JobAuthenticityService.audit(self.job, use_llm=False)

        response = self.client.get(f"/api/resume/jobs/{self.job.id}/authenticity")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["authenticity"]["authenticity_verdict"], "PERM_COMPLIANCE")
        self.assertEqual(data["authenticity"]["authenticity_score"], 40)

    def test_post_audit_authenticity_api_endpoint(self):
        """POST /api/resume/jobs/{id}/audit-authenticity triggers on-demand audit and updates record."""
        fresh_job = JobListing.objects.create(
            title="Platform Architect",
            company_name="Startup Inc",
            location="Plano, TX",
            external_id="ext-test-2",
            description="Requirements: Java, Python, or Go or equivalent experience.",
            source="manual",
        )
        self.assertEqual(fresh_job.authenticity_verdict, "")

        with patch("resume_app.llm.gateway.local_llm_available", return_value=False):
            response = self.client.post(
                f"/api/resume/jobs/{fresh_job.id}/audit-authenticity",
                content_type="application/json"
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["authenticity"]["authenticity_verdict"], "GENUINE_EXTERNAL")

        fresh_job.refresh_from_db()
        self.assertEqual(fresh_job.authenticity_verdict, "GENUINE_EXTERNAL")
