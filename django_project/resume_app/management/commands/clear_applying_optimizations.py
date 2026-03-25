"""
One-time cleanup: delete OptimizedResume rows (and orphan JobDescriptions) for
pipeline entries in Applying, so re-enqueue behaves like a fresh run.

Run from django_project:
  python manage.py clear_applying_optimizations
  python manage.py clear_applying_optimizations --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from resume_app.models import JobDescription, OptimizedResume, PipelineEntry


class Command(BaseCommand):
    help = (
        "Delete resume optimizations tied to Applying-stage pipeline entries "
        "(AgentLogs cascade; orphan JobDescriptions removed)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print counts only; do not delete.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        applying = PipelineEntry.objects.filter(
            stage=PipelineEntry.Stage.APPLYING,
            removed_at__isnull=True,
        )
        qs = OptimizedResume.objects.filter(pipeline_entry__in=applying).select_related(
            "pipeline_entry"
        )
        n = qs.count()
        if n == 0:
            self.stdout.write(self.style.SUCCESS("No OptimizedResume rows for Applying entries."))
            return

        self.stdout.write(f"Applying-stage entries (active): {applying.count()}")
        self.stdout.write(f"OptimizedResume rows to remove: {n}")
        if dry:
            self.stdout.write(self.style.WARNING("Dry run — no changes."))
            return

        jd_ids = list(qs.values_list("job_description_id", flat=True))
        with transaction.atomic():
            deleted, _ = qs.delete()
        # qs.delete() returns total including cascaded AgentLog rows
        self.stdout.write(self.style.SUCCESS(f"Deleted OptimizedResume (+ cascades): {deleted}"))

        orphan_jd = JobDescription.objects.filter(id__in=jd_ids).exclude(
            id__in=OptimizedResume.objects.values_list("job_description_id", flat=True)
        )
        jd_n = orphan_jd.count()
        if jd_n:
            orphan_jd.delete()
            self.stdout.write(self.style.SUCCESS(f"Removed {jd_n} orphan JobDescription row(s)."))
        else:
            self.stdout.write("No orphan JobDescription rows.")
