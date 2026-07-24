"""Normalize/dedupe User.email and report conflicts before uniqueness is enforced."""
from django.core.management.base import BaseCommand

from resume_app.account import dedupe_user_emails


class Command(BaseCommand):
    help = (
        "Normalize emails to lowercase and clear duplicates (losers get blank email). "
        "Migration 0025 runs the same cleanup; use --dry-run to preview."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print planned actions without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        actions = dedupe_user_emails(dry_run=dry_run)
        if not actions:
            self.stdout.write(self.style.SUCCESS("No email changes needed."))
            return
        for row in actions:
            self.stdout.write(str(row))
        label = "Would apply" if dry_run else "Applied"
        self.stdout.write(self.style.SUCCESS(f"{label} {len(actions)} action(s)."))
