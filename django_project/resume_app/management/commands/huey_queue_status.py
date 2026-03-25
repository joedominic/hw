"""
Show Huey queue status: pending (main queue), scheduled, and result store size.
Run from django_project: python manage.py huey_queue_status
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print Huey queue status (pending, scheduled, results). Requires Redis."

    def handle(self, *args, **options):
        try:
            from huey.contrib.djhuey import HUEY
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Huey not available: {e}"))
            return
        if getattr(HUEY, "immediate", False):
            self.stdout.write("Huey is in immediate mode (no Redis queue). Tasks run in-process.")
            return
        try:
            storage = HUEY.storage
            pending = storage.queue_size()
            scheduled = storage.schedule_size()
            results = storage.result_store_size()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Could not read queue (is Redis running?): {e}"))
            return
        self.stdout.write(
            f"Pending (main queue): {pending}  |  Scheduled: {scheduled}  |  Results in store: {results}"
        )
        self.stdout.write(
            "Start the consumer to process the queue: python manage.py run_huey"
        )
