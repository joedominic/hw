"""
Management command to inspect and control job source circuit breakers.
Usage:
    python manage.py source_circuit_breakers
    python manage.py source_circuit_breakers --reset indeed
    python manage.py source_circuit_breakers --reset all
"""
from django.core.management.base import BaseCommand
from resume_app.sourcing.circuit_breaker import (
    get_all_sources_status,
    get_circuit_breaker,
)
from resume_app.job_sources import ALLOWED_SITE_NAMES


class Command(BaseCommand):
    help = "Inspect and manage external job source circuit breakers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            type=str,
            help="Reset circuit breaker for a specific source, or 'all' to reset all.",
        )

    def handle(self, *args, **options):
        reset_target = options.get("reset")
        if reset_target:
            target = reset_target.strip().lower()
            if target == "all":
                for s in ALLOWED_SITE_NAMES:
                    get_circuit_breaker(s).reset()
                self.stdout.write(self.style.SUCCESS("Reset all job source circuit breakers to CLOSED."))
            else:
                get_circuit_breaker(target).reset()
                self.stdout.write(self.style.SUCCESS(f"Reset circuit breaker for '{target}' to CLOSED."))
            return

        statuses = get_all_sources_status()
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Job Source Circuit Breaker Status ==="))
        headers = f"{'Source':<12} | {'State':<10} | {'Available':<10} | {'Admin Off':<10} | {'Failures':<8} | {'Cooldown':<10} | {'Last Error'}"
        self.stdout.write(headers)
        self.stdout.write("-" * 80)

        for s in statuses:
            avail_str = "YES" if s["is_available"] else "NO"
            admin_str = "YES" if s["is_admin_disabled"] else "NO"
            cooldown_str = f"{s['cooldown_remaining_seconds']}s" if s["cooldown_remaining_seconds"] > 0 else "—"
            err_str = s["last_error"][:35] + "..." if len(s["last_error"]) > 35 else (s["last_error"] or "—")

            line = f"{s['source']:<12} | {s['state']:<10} | {avail_str:<10} | {admin_str:<10} | {s['failure_count']:<8} | {cooldown_str:<10} | {err_str}"
            if s["state"] == "OPEN" or s["is_admin_disabled"]:
                self.stdout.write(self.style.WARNING(line))
            elif s["state"] == "HALF_OPEN":
                self.stdout.write(self.style.NOTICE(line))
            else:
                self.stdout.write(self.style.SUCCESS(line))
        self.stdout.write("\n")
