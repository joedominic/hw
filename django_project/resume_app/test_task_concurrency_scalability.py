from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from resume_app.models import (
    AppAutomationSettings,
    JobListing,
    JobSearchTask,
    PipelineEntry,
    UserResume,
)
from resume_app.tasks import (
    cleanup_manager,
    enqueue_due_job_search_tasks,
    enqueue_due_vetting_matching_tasks,
    evaluate_vetting_matching_task,
    get_job_search_task_lock_key,
    get_vetting_matching_lock_key,
    pipeline_manager,
    process_user_cleanup_task,
    process_user_pipeline_manager_task,
    process_user_purge_generated_resumes_task,
    process_user_vetting_matching_task,
    purge_generated_resumes_periodic,
    run_job_search_task,
)
from resume_app.job_search_core import (
    get_interactive_job_search_lock_key,
    run_job_search_core,
)

User = get_user_model()


class TaskConcurrencyLockingTests(TestCase):
    """Verify that task locks are isolated per-tenant/per-user and allow concurrent execution."""

    def setUp(self):
        cache.clear()
        self.user_a = User.objects.create_user(username="tenant_a", password="pass12345!")
        self.user_b = User.objects.create_user(username="tenant_b", password="pass12345!")

        self.task_a = JobSearchTask.objects.create(
            owner=self.user_a,
            search_term="Python Developer",
            frequency="0 9 * * *",
            is_active=True,
        )
        self.task_b = JobSearchTask.objects.create(
            owner=self.user_b,
            search_term="Frontend Engineer",
            frequency="0 9 * * *",
            is_active=True,
        )

    def tearDown(self):
        cache.clear()

    def test_job_search_lock_is_per_user(self):
        """User A's running search must NOT block User B's search."""
        lock_a = get_job_search_task_lock_key(self.user_a.id)
        lock_b = get_job_search_task_lock_key(self.user_b.id)

        self.assertNotEqual(lock_a, lock_b)
        self.assertEqual(lock_a, f"job_search_task_running:u{self.user_a.id}")
        self.assertEqual(lock_b, f"job_search_task_running:u{self.user_b.id}")

        # Simulate User A acquiring lock
        cache.set(lock_a, 1, 300)

        # User A's second task should be skipped
        res_a = run_job_search_task.call_local(self.user_a.id, self.task_a.id)
        self.assertEqual(res_a["status"], "skipped")
        self.assertIn("Another job search task is running for this user", res_a["message"])

        # User B's task should NOT be blocked by User A's lock
        with patch("resume_app.tasks._run_job_search_task_impl", return_value={"status": "success", "task_id": self.task_b.id}) as mock_impl:
            res_b = run_job_search_task.call_local(self.user_b.id, self.task_b.id)
            self.assertEqual(res_b["status"], "success")
            mock_impl.assert_called_once_with(self.user_b.id, self.task_b.id)

    def test_vetting_matching_lock_is_per_user(self):
        """User A's running vetting matching must NOT block User B's vetting matching."""
        lock_a = get_vetting_matching_lock_key(self.user_a.id)
        lock_b = get_vetting_matching_lock_key(self.user_b.id)

        self.assertNotEqual(lock_a, lock_b)
        self.assertEqual(lock_a, f"vetting_matching_task_running:u{self.user_a.id}")
        self.assertEqual(lock_b, f"vetting_matching_task_running:u{self.user_b.id}")

        # Simulate User A acquiring lock
        cache.set(lock_a, 1, 300)

        # User A should be skipped
        res_a = evaluate_vetting_matching_task.call_local(self.user_a.id, [1, 2])
        self.assertEqual(res_a["status"], "skipped")
        self.assertIn("Another vetting matching task is running for this user", res_a["message"])

        # User B should not be blocked
        res_b = evaluate_vetting_matching_task.call_local(self.user_b.id, [])
        # Returns skipped because entry list is empty, not because of a lock conflict
        self.assertEqual(res_b["status"], "skipped")
        self.assertEqual(res_b["message"], "No pipeline_entry_ids provided")

    def test_interactive_job_search_lock_is_per_user(self):
        """Interactive job search locks must be isolated per tenant."""
        lock_a = get_interactive_job_search_lock_key(self.user_a.id)
        lock_b = get_interactive_job_search_lock_key(self.user_b.id)

        self.assertNotEqual(lock_a, lock_b)
        self.assertEqual(lock_a, f"job_search_interactive:u{self.user_a.id}")
        self.assertEqual(lock_b, f"job_search_interactive:u{self.user_b.id}")

        # Simulate User A having an in-flight search
        cache.set(lock_a, 1, 60)

        # User A's concurrent search should raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            run_job_search_core(user=self.user_a, search_term="Python")
        self.assertIn("already in progress", str(ctx.exception))

        # User B's search must NOT be blocked by User A's lock
        with patch("resume_app.job_search_core.fetch_jobs", return_value=[]):
            fetched, after_filter, jobs_out, refs = run_job_search_core(user=self.user_b, search_term="Python")
            self.assertEqual(fetched, 0)
            self.assertEqual(len(jobs_out), 0)


class WorkerScalabilityFanOutTests(TestCase):
    """Verify that periodic tasks fan out to per-user worker tasks instead of monolithic loops."""

    def setUp(self):
        cache.clear()
        self.user_1 = User.objects.create_user(username="scale_user_1", password="pass12345!")
        self.user_2 = User.objects.create_user(username="scale_user_2", password="pass12345!")

    def tearDown(self):
        cache.clear()

    @patch("resume_app.tasks.process_user_pipeline_manager_task")
    def test_pipeline_manager_fans_out(self, mock_task):
        res = pipeline_manager.call_local()
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "dispatched")

        # Must have dispatched task for each active user
        called_user_ids = {call.args[0] for call in mock_task.call_args_list}
        self.assertIn(self.user_1.id, called_user_ids)
        self.assertIn(self.user_2.id, called_user_ids)

    @patch("resume_app.tasks.process_user_vetting_matching_task")
    def test_enqueue_due_vetting_matching_tasks_fans_out(self, mock_task):
        res = enqueue_due_vetting_matching_tasks.call_local()
        self.assertEqual(res["status"], "dispatched")

        called_user_ids = {call.args[0] for call in mock_task.call_args_list}
        self.assertIn(self.user_1.id, called_user_ids)
        self.assertIn(self.user_2.id, called_user_ids)

    @patch("resume_app.tasks.process_user_cleanup_task")
    def test_cleanup_manager_fans_out(self, mock_cleanup):
        res = cleanup_manager.call_local()
        self.assertEqual(res["status"], "dispatched")

        called_user_ids = {call.args[0] for call in mock_cleanup.call_args_list}
        self.assertIn(self.user_1.id, called_user_ids)
        self.assertIn(self.user_2.id, called_user_ids)

    @patch("resume_app.tasks.process_user_purge_generated_resumes_task")
    def test_purge_generated_resumes_periodic_fans_out(self, mock_purge):
        res = purge_generated_resumes_periodic.call_local()
        self.assertEqual(res["status"], "dispatched")

        called_user_ids = {call.args[0] for call in mock_purge.call_args_list}
        self.assertIn(self.user_1.id, called_user_ids)
        self.assertIn(self.user_2.id, called_user_ids)

    @patch("resume_app.tasks.run_job_search_task")
    def test_enqueue_due_job_search_tasks_batches_due_tasks(self, mock_search):
        now = timezone.now()
        past = now - timedelta(minutes=5)

        task_1 = JobSearchTask.objects.create(
            owner=self.user_1,
            search_term="Python 1",
            frequency="0 9 * * *",
            is_active=True,
            next_run_at=past,
        )
        task_2 = JobSearchTask.objects.create(
            owner=self.user_2,
            search_term="Python 2",
            frequency="0 9 * * *",
            is_active=True,
            next_run_at=past,
        )

        res = enqueue_due_job_search_tasks.call_local()
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["enqueued"], 2)

        # Both due tasks should have been enqueued in the same tick
        enqueued_task_ids = {call.args[1] for call in mock_search.call_args_list}
        self.assertIn(task_1.id, enqueued_task_ids)
        self.assertIn(task_2.id, enqueued_task_ids)
