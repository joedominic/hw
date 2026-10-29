"""
Management command to inspect and manage the Dead-Letter Queue (DLQ).
Usage:
    python manage.py task_dlq --list
    python manage.py task_dlq --replay <id>
    python manage.py task_dlq --dismiss <id>
    python manage.py task_dlq --purge
"""
from django.core.management.base import BaseCommand
from resume_app.models import FailedTaskQuarantine
from resume_app.task_dlq import purge_old_quarantine_records, replay_quarantined_task
from django.utils import timezone


class Command(BaseCommand):
    help = "Inspect and manage quarantined background worker tasks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="List all quarantined tasks.",
        )
        parser.add_argument(
            "--replay",
            type=int,
            metavar="ID",
            help="Replay a quarantined task by record ID.",
        )
        parser.add_argument(
            "--dismiss",
            type=int,
            metavar="ID",
            help="Dismiss a quarantined task without replaying.",
        )
        parser.add_argument(
            "--purge",
            action="store_true",
            help="Purge dismissed/replayed records older than 30 days.",
        )

    def handle(self, *args, **options):
        if options.get("replay"):
            rec_id = options["replay"]
            success = replay_quarantined_task(rec_id)
            if success:
                self.stdout.write(self.style.SUCCESS(f"Successfully replayed task #{rec_id}."))
            else:
                self.stdout.write(self.style.ERROR(f"Failed to replay task #{rec_id}. Check logs."))
            return

        if options.get("dismiss"):
            rec_id = options["dismiss"]
            record = FailedTaskQuarantine.objects.filter(pk=rec_id).first()
            if not record:
                self.stdout.write(self.style.ERROR(f"Task record #{rec_id} not found."))
                return
            record.status = FailedTaskQuarantine.Status.DISMISSED
            record.resolved_at = timezone.now()
            record.save(update_fields=["status", "resolved_at"])
            self.stdout.write(self.style.SUCCESS(f"Task #{rec_id} marked as DISMISSED."))
            return

        if options.get("purge"):
            deleted = purge_old_quarantine_records(days=30)
            self.stdout.write(self.style.SUCCESS(f"Purged {deleted} old DLQ records."))
            return

        # Default action: list
        records = FailedTaskQuarantine.objects.all()[:50]
        if not records:
            self.stdout.write(self.style.SUCCESS("\nDead-Letter Queue is empty. Zero quarantined tasks.\n"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Dead-Letter Queue (Failed Tasks) ==="))
        headers = f"{'ID':<6} | {'Status':<12} | {'Task Name':<35} | {'Error Class':<20} | {'Created At'}"
        self.stdout.write(headers)
        self.stdout.write("-" * 95)

        for r in records:
            dt_str = r.created_at.strftime("%Y-%m-%d %H:%M:%S")
            short_name = r.task_name[-35:] if len(r.task_name) > 35 else r.task_name
            short_err = r.error_class[:20]

            line = f"{r.id:<6} | {r.status:<12} | {short_name:<35} | {short_err:<20} | {dt_str}"
            if r.status == FailedTaskQuarantine.Status.QUARANTINED:
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(self.style.NOTICE(line))
        self.stdout.write("\n")
