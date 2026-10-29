"""
Unit and integration tests for background worker reliability (Huey leader election, DLQ quarantine)
and job scraper resilience (circuit breaker, graceful partial aggregation, admin disables).
"""
import time
from unittest.mock import MagicMock, patch
from django.core.cache import cache
from django.test import TestCase, override_settings

from resume_app.models import FailedTaskQuarantine
from resume_app.sourcing.circuit_breaker import (
    CircuitBreakerOpenException,
    SourceCircuitBreaker,
    get_circuit_breaker,
    is_source_enabled,
)
from resume_app.task_dlq import (
    purge_old_quarantine_records,
    quarantine_failed_task,
    replay_quarantined_task,
)
from resume_app.tasks import acquire_periodic_leader_lock


class JobSourceCircuitBreakerTest(TestCase):
    def setUp(self):
        cache.clear()
        self.cb = SourceCircuitBreaker("testboard", failure_threshold=2, cooldown_seconds=2)
        self.cb.reset()

    def tearDown(self):
        self.cb.reset()
        cache.clear()

    def test_circuit_closed_by_default_and_executes_successfully(self):
        """Healthy circuit allows calls and records success."""
        mock_fn = MagicMock(return_value=[{"title": "SWE"}])
        result = self.cb.call(mock_fn, 123)

        self.assertEqual(result, [{"title": "SWE"}])
        mock_fn.assert_called_once_with(123)
        status = self.cb.get_status()
        self.assertEqual(status["state"], "CLOSED")
        self.assertEqual(status["failure_count"], 0)
        self.assertTrue(self.cb.is_available())

    def test_circuit_trips_open_after_reaching_failure_threshold(self):
        """Repeated failures trip the circuit breaker to OPEN, fast-failing subsequent calls."""
        fail_fn = MagicMock(side_effect=RuntimeError("Connection timeout"))

        # First failure
        with self.assertRaises(RuntimeError):
            self.cb.call(fail_fn)
        self.assertEqual(self.cb.get_status()["failure_count"], 1)
        self.assertEqual(self.cb.get_status()["state"], "CLOSED")

        # Second failure -> trips OPEN
        with self.assertRaises(RuntimeError):
            self.cb.call(fail_fn)

        status = self.cb.get_status()
        self.assertEqual(status["state"], "OPEN")
        self.assertFalse(self.cb.is_available())
        self.assertIn("Connection timeout", status["last_error"])

        # Third attempt must fast-fail with CircuitBreakerOpenException without invoking fail_fn again
        fail_fn.reset_mock()
        with self.assertRaises(CircuitBreakerOpenException) as cm:
            self.cb.call(fail_fn)

        self.assertIn("is OPEN", str(cm.exception))
        fail_fn.assert_not_called()

    def test_circuit_recovers_to_half_open_after_cooldown(self):
        """After cooldown passes, circuit enters HALF_OPEN and allows probe trial."""
        self.cb.record_failure("Failure 1")
        self.cb.record_failure("Failure 2")
        self.assertEqual(self.cb.get_status()["state"], "OPEN")

        # Manually backdate failure time to simulate cooldown expiration
        data = self.cb._get_state_data()
        data["last_failure_time"] = time.time() - 10
        self.cb._save_state_data(data)

        # Should now be available (HALF_OPEN trial probe)
        self.assertTrue(self.cb.is_available())
        self.assertEqual(self.cb.get_status()["state"], "HALF_OPEN")

        # Probe succeeds -> resets to CLOSED
        probe_fn = MagicMock(return_value="recovered")
        res = self.cb.call(probe_fn)
        self.assertEqual(res, "recovered")
        self.assertEqual(self.cb.get_status()["state"], "CLOSED")
        self.assertEqual(self.cb.get_status()["failure_count"], 0)

    @override_settings(DISABLED_JOB_SOURCES=["dice", "testboard"])
    def test_administratively_disabled_source_blocks_immediately(self):
        """Sources listed in DISABLED_JOB_SOURCES are immediately unavailable."""
        self.assertFalse(self.cb.is_available())
        self.assertTrue(self.cb.get_status()["is_admin_disabled"])


class JobSearchGracefulDegradationTest(TestCase):
    def setUp(self):
        cache.clear()

    @patch("resume_app.job_sources._fetch_jobs_jobspy")
    @patch("resume_app.sourcing.clients.dice_client.fetch_dice_jobs")
    def test_multi_source_search_returns_partial_results_when_one_fails(
        self, mock_dice, mock_jobspy
    ):
        """When Indeed times out or throws, Dice results are still returned cleanly."""
        from resume_app.job_sources import fetch_jobs

        # Indeed fails catastrophically
        mock_jobspy.side_effect = RuntimeError("Indeed Cloudflare 403 Forbidden")

        # Dice succeeds
        mock_dice.return_value = [
            {
                "title": "Principal SRE",
                "company_name": "CloudScale",
                "location": "Remote",
                "description": "Kubernetes and Terraform",
                "job_url": "https://dice.com/job/1",
                "source": "dice",
                "external_id": "dice-sre-1",
            }
        ]

        results = fetch_jobs(
            search_term="Principal SRE",
            site_name=["indeed", "dice"],
            results_wanted=10,
        )

        # Assert Dice results returned rather than crashing whole search
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Principal SRE")
        self.assertEqual(results[0]["source"], "dice")

        # Verify Indeed circuit breaker recorded failure
        indeed_cb = get_circuit_breaker("indeed")
        self.assertGreaterEqual(indeed_cb.get_status()["failure_count"], 1)

        # Verify Dice circuit breaker recorded success
        dice_cb = get_circuit_breaker("dice")
        self.assertEqual(dice_cb.get_status()["state"], "CLOSED")


class WorkerReliabilityAndDLQTest(TestCase):
    def setUp(self):
        cache.clear()
        FailedTaskQuarantine.objects.all().delete()

    def test_periodic_leader_lock_prevents_duplicate_runs_across_replicas(self):
        """Leader lock ensures exactly one replica acquires execution token per cycle."""
        # First replica acquires lock
        is_leader_1 = acquire_periodic_leader_lock("pipeline_manager", lock_ttl_seconds=60)
        self.assertTrue(is_leader_1)

        # Second replica attempts during same cycle -> rejected
        is_leader_2 = acquire_periodic_leader_lock("pipeline_manager", lock_ttl_seconds=60)
        self.assertFalse(is_leader_2)

    def test_failed_task_quarantine_records_exception_and_traceback(self):
        """Uncaught background exceptions are saved to DLQ table with full diagnostics."""
        try:
            raise ValueError("LLM provider rate limit exceeded")
        except ValueError as exc:
            record = quarantine_failed_task(
                task_id="huey-task-dlq-1",
                task_name="resume_app.tasks.optimize_resume_task",
                args=(10, 20),
                kwargs={"debug": True},
                exc=exc,
            )

        self.assertIsNotNone(record)
        self.assertEqual(record.status, FailedTaskQuarantine.Status.QUARANTINED)
        self.assertEqual(record.task_name, "resume_app.tasks.optimize_resume_task")
        self.assertEqual(record.error_class, "ValueError")
        self.assertIn("LLM provider rate limit", record.error_message)
        self.assertIn("ValueError", record.traceback)

    def test_dlq_purge_cleans_only_resolved_records(self):
        """Purge removes dismissed/replayed records but preserves quarantined records."""
        # Quarantined record
        r1 = quarantine_failed_task("task-1", "test.task", exc=RuntimeError("err1"))
        # Dismissed record
        r2 = quarantine_failed_task("task-2", "test.task", exc=RuntimeError("err2"))
        r2.status = FailedTaskQuarantine.Status.DISMISSED
        r2.save()

        # Set creation to 40 days ago
        old_time = time.time() - (40 * 86400)
        from django.utils import timezone
        from datetime import timedelta
        FailedTaskQuarantine.objects.filter(pk__in=[r1.id, r2.id]).update(
            created_at=timezone.now() - timedelta(days=40)
        )

        purged_count = purge_old_quarantine_records(days=30)
        self.assertEqual(purged_count, 1)

        # r1 still exists because it's QUARANTINED
        self.assertTrue(FailedTaskQuarantine.objects.filter(pk=r1.id).exists())
        # r2 was deleted because it's DISMISSED
        self.assertFalse(FailedTaskQuarantine.objects.filter(pk=r2.id).exists())
