"""Fail optimizer and job-search runs whose worker stopped."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from resume_app.tasks import mark_stale_job_search_runs_failed, mark_stale_optimizations_failed


class Command(BaseCommand):
    help = "Mark abandoned optimizer and job-search runs as failed. Use after stopping run_huey."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count stale rows without updating them.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            from datetime import timedelta

            from django.conf import settings
            from django.utils import timezone

            from resume_app.models import JobSearchTaskRun, OptimizedResume

            opt_cutoff = timezone.now() - timedelta(minutes=int(getattr(settings, "OPTIMIZER_STALE_MINUTES", 20)))
            search_cutoff = timezone.now() - timedelta(minutes=60)
            opt_count = OptimizedResume.objects.filter(
                status__in=(OptimizedResume.STATUS_QUEUED, OptimizedResume.STATUS_RUNNING),
                updated_at__lt=opt_cutoff,
            ).count()
            search_count = JobSearchTaskRun.objects.filter(
                status=JobSearchTaskRun.STATUS_RUNNING,
                started_at__lt=search_cutoff,
            ).count()
            self.stdout.write(f"dry-run optimizations={opt_count} job_search_runs={search_count}")
            return

        # Call the bodies directly. The periodic wrappers take the leader lock.
        failed = mark_stale_optimizations_failed()
        mark_stale_job_search_runs_failed()
        self.stdout.write(self.style.SUCCESS(f"Reaped optimizer runs: {failed}"))
