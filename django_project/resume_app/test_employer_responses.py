"""Tests for Employer Responses & Post-Apply Interview Milestone Tracking."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from resume_app.dashboard_stats import get_performance_dashboard_stats
from resume_app.models import EmployerInterviewEvent, JobListing, PipelineEntry, SearchProfile

User = get_user_model()


class EmployerResponseTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testcandidate", email="cand@example.com", password="pw")
        self.profile = SearchProfile.objects.create(
            owner=self.user,
            name="Default",
            slug="default",
            profile_slug="default",
            is_default=True,
        )
        self.job1 = JobListing.objects.create(
            source="manual", external_id="job_1", title="Staff Engineer", company_name="Stripe"
        )
        self.job2 = JobListing.objects.create(
            source="manual", external_id="job_2", title="Director of Eng", company_name="Coinbase"
        )

        self.entry1 = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job1,
            track="default",
            stage=PipelineEntry.Stage.DONE,
            applied_at=timezone.now(),
        )
        self.entry2 = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=self.job2,
            track="default",
            stage=PipelineEntry.Stage.DONE,
            applied_at=timezone.now(),
        )

    def test_default_post_apply_substatus(self):
        self.assertEqual(self.entry1.post_apply_substatus, PipelineEntry.PostApplySubstatus.APPLIED_PENDING)
        stats = get_performance_dashboard_stats(self.user)
        self.assertEqual(stats["outbound_response_rate"], "0.0%")
        self.assertEqual(stats["recruiter_screens_booked"], 0)
        self.assertEqual(stats["milestones"]["first_round"], 0)

    def test_log_interview_event_and_stats_update(self):
        # Update entry 1 to Recruiter Call
        scheduled_time = timezone.now() + timezone.timedelta(days=1)
        self.entry1.update_post_apply_substatus(
            PipelineEntry.PostApplySubstatus.RECRUITER_CALL,
            next_interview_at=scheduled_time,
        )
        event = EmployerInterviewEvent.objects.create(
            owner=self.user,
            pipeline_entry=self.entry1,
            round_type=PipelineEntry.PostApplySubstatus.RECRUITER_CALL,
            status=EmployerInterviewEvent.Status.SCHEDULED,
            scheduled_at=scheduled_time,
            interviewer_names="Jane Recruiter",
            location_or_link="Zoom URL",
            notes="Focus on platform architecture.",
        )

        # Update entry 2 to Panel Interview
        self.entry2.update_post_apply_substatus(
            PipelineEntry.PostApplySubstatus.PANEL_INTERVIEW,
        )

        stats = get_performance_dashboard_stats(self.user)
        # 2 out of 2 jobs responded = 100.0%
        self.assertEqual(stats["outbound_response_rate"], "100.0%")
        self.assertEqual(stats["milestones"]["first_round"], 1)
        self.assertEqual(stats["milestones"]["tech_deep_dive"], 1)
        self.assertEqual(stats["milestones"]["finalist_round"], 0)

        # Verify screen cards
        self.assertGreaterEqual(len(stats["confirmed_screens"]), 2)
        stripe_screen = next(s for s in stats["confirmed_screens"] if s["company"] == "Stripe")
        self.assertEqual(stripe_screen["initials"], "S")
        self.assertEqual(stripe_screen["role"], "Staff Engineer")
        self.assertIn("Recruiter Screen", stripe_screen["stage_label"])
        self.assertEqual(stripe_screen["action_url"], f"/jobs/cockpit/?track=default&stage=done&job_id={self.job1.id}")

        # Verify events in the past are NOT shown in confirmed_screens
        past_time = timezone.now() - timezone.timedelta(days=3)
        past_job = JobListing.objects.create(
            source="manual", external_id="job_past", title="Past Role", company_name="PastCo"
        )
        past_entry = PipelineEntry.objects.create(
            owner=self.user,
            job_listing=past_job,
            track="default",
            stage=PipelineEntry.Stage.DONE,
            applied_at=past_time,
            post_apply_substatus=PipelineEntry.PostApplySubstatus.RECRUITER_CALL,
            next_interview_at=past_time,
        )
        past_event = EmployerInterviewEvent.objects.create(
            owner=self.user,
            pipeline_entry=past_entry,
            round_type=PipelineEntry.PostApplySubstatus.RECRUITER_CALL,
            status=EmployerInterviewEvent.Status.SCHEDULED,
            scheduled_at=past_time,
            interviewer_names="Past Recruiter",
        )
        stats_after = get_performance_dashboard_stats(self.user)
        companies = [s["company"] for s in stats_after["confirmed_screens"]]
        self.assertNotIn("PastCo", companies)

    def test_api_applied_jobs_endpoint(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/resume/jobs/pipeline/applied-jobs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 2)
        titles = [i["title"] for i in data["items"]]
        self.assertIn("Staff Engineer", titles)
        self.assertIn("Director of Eng", titles)

    def test_api_log_interview_event_endpoint(self):
        self.client.force_login(self.user)
        payload = {
            "round_type": "hiring_manager",
            "status": "scheduled",
            "scheduled_at": (timezone.now() + timezone.timedelta(days=2)).isoformat(),
            "duration_minutes": 60,
            "interviewer_names": "Bob VP",
            "location_or_link": "https://meet.google.com/xyz",
            "notes": "Deep dive into system scale.",
            "generate_prep": False,
        }
        response = self.client.post(
            f"/api/resume/jobs/pipeline-entry/{self.entry1.id}/log-interview",
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["substatus"], "hiring_manager")

        self.entry1.refresh_from_db()
        self.assertEqual(self.entry1.post_apply_substatus, "hiring_manager")
        self.assertIsNotNone(self.entry1.next_interview_at)

        event = EmployerInterviewEvent.objects.filter(pipeline_entry=self.entry1).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.interviewer_names, "Bob VP")
        self.assertEqual(event.duration_minutes, 60)

    def test_cockpit_done_stage_log_interview_integration(self):
        from resume_app.pipeline_board import _attach_optimized_resume_ids_for_stage

        jobs = [self.job1]
        _attach_optimized_resume_ids_for_stage(self.user, jobs, "default", PipelineEntry.Stage.DONE)
        self.assertEqual(getattr(self.job1, "pipeline_entry_id", None), self.entry1.id)
        self.assertEqual(getattr(self.job1, "post_apply_substatus", None), PipelineEntry.PostApplySubstatus.APPLIED_PENDING)

        self.client.force_login(self.user)
        response = self.client.get("/jobs/cockpit/?stage=done")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        # Verify drawer markup is present in cockpit
        self.assertIn('id="log-interview-drawer"', content)
        self.assertIn('id="log-interview-backdrop"', content)
        # Verify Log Response button calls openLogInterviewDrawer with entry id
        self.assertIn(f"openLogInterviewDrawer('{self.entry1.id}')", content)

        # 1. Verify Tailor button is NOT rendered in stage=done
        self.assertNotIn('<span>Tailor</span>', content)
        self.assertNotIn('id="trigger-tailor-btn"', content)
        # 2. Verify Prep Kit button is rendered instead
        self.assertIn('Prep Kit', content)
        self.assertIn('trigger-prep-kit-btn', content)
        # 3. Verify Pane 3 has Employer Response and Interview Prep Kit cards
        self.assertIn('id="inspect-interview-response-card"', content)
        self.assertIn('id="inspect-interview-prep-card"', content)

    def test_log_interview_event_invalidates_cache_and_returns_full_payload(self):
        from django.core.cache import cache

        cache_key = f"perf_stats_{self.user.id}"
        cache.set(cache_key, {"dummy": True}, timeout=120)

        self.client.force_login(self.user)
        payload = {
            "round_type": "recruiter_call",
            "status": "scheduled",
            "scheduled_at": (timezone.now() + timezone.timedelta(days=1)).isoformat(),
            "duration_minutes": 30,
            "interviewer_names": "Sarah Connor",
            "location_or_link": "https://zoom.us/j/12345",
            "notes": "Discuss initial comp expectations.",
            "generate_prep": False,
        }
        response = self.client.post(
            f"/api/resume/jobs/pipeline-entry/{self.entry1.id}/log-interview",
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["interviewer_names"], "Sarah Connor")
        self.assertEqual(data["location_or_link"], "https://zoom.us/j/12345")
        self.assertEqual(data["notes"], "Discuss initial comp expectations.")
        self.assertEqual(data["duration_minutes"], 30)

        # Cache must be invalidated
        self.assertIsNone(cache.get(cache_key))

        # applied-jobs API must now return the event
        get_res = self.client.get("/api/resume/jobs/pipeline/applied-jobs")
        self.assertEqual(get_res.status_code, 200)
        applied_items = get_res.json()["items"]
        item1 = next(i for i in applied_items if i["pipeline_entry_id"] == self.entry1.id)
        self.assertIsNotNone(item1["event"])
        self.assertEqual(item1["event"]["interviewer_names"], "Sarah Connor")
        self.assertEqual(item1["event"]["location_or_link"], "https://zoom.us/j/12345")


