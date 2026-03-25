"""
Remove duplicate pipeline rows (same job, multiple locations) for a track/stage scope.

Run from django_project:
  python manage.py dedupe_pipeline_jobs
  python manage.py dedupe_pipeline_jobs --track ic --stage pipeline
  python manage.py dedupe_pipeline_jobs --track all --stage all --include-done
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Deduplicate pipeline entries: same title/company/description per track, keep best-scoring row."

    def add_arguments(self, parser):
        parser.add_argument(
            "--track",
            default="*",
            help="Track slug, or * / all for every track (default: *).",
        )
        parser.add_argument(
            "--stage",
            default="all",
            help="pipeline | vetting | applying | done | all (default: all = pipeline+vetting+applying).",
        )
        parser.add_argument(
            "--include-done",
            action="store_true",
            help="When --stage is all, also consider Done rows.",
        )

    def handle(self, *args, **options):
        from resume_app.job_dedupe import dedupe_pipeline_entries

        track = (options["track"] or "*").strip().lower()
        stage = (options["stage"] or "all").strip().lower()
        include_done = bool(options["include_done"])

        try:
            result = dedupe_pipeline_entries(
                track_slug=track,
                stage=stage,
                include_done=include_done,
            )
        except ValueError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        if result.get("status") != "success":
            self.stderr.write(self.style.ERROR(str(result)))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {result['entries_removed']} row(s) in {result['duplicate_groups']} duplicate group(s)."
            )
        )
        for slug, info in (result.get("per_track") or {}).items():
            if info.get("removed"):
                self.stdout.write(f"  {slug}: removed {info['removed']} in {info['duplicate_groups']} groups")
