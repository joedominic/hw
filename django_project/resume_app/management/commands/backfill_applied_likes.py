from django.core.management.base import BaseCommand
from django.db import transaction
from resume_app.models import PipelineEntry, JobListingAction
from resume_app.track_actions import record_job_liked


class Command(BaseCommand):
    help = "Backfill LIKED actions and preference embeddings for all jobs currently in the Applied (Done) stage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sync-embeddings",
            action="store_true",
            default=False,
            help="Compute and save preference embeddings synchronously rather than in background threads.",
        )

    def handle(self, *args, **options):
        sync_embeddings = options.get("sync_embeddings", False)
        applied_entries = (
            PipelineEntry.objects.filter(stage=PipelineEntry.Stage.DONE, removed_at__isnull=True)
            .select_related("owner", "job_listing", "search_profile")
        )
        total = applied_entries.count()
        self.stdout.write(f"Found {total} jobs currently in Applied (Done) stage.")

        backfilled_count = 0
        already_liked_count = 0

        for entry in applied_entries:
            if not entry.owner or not entry.job_listing:
                continue

            has_like = JobListingAction.objects.filter(
                owner=entry.owner,
                job_listing=entry.job_listing,
                action=JobListingAction.ActionType.LIKED,
                track=entry.track,
            ).exists()

            if has_like:
                already_liked_count += 1
                continue

            try:
                record_job_liked(
                    user=entry.owner,
                    job=entry.job_listing,
                    track=entry.track,
                    search_profile=entry.search_profile,
                    sync_embedding=sync_embeddings,
                )
                backfilled_count += 1
                self.stdout.write(
                    f"Tagged job #{entry.job_listing_id} ({entry.job_listing.title[:40]}) as LIKED for user {entry.owner.username} [track={entry.track}]."
                )
            except Exception as e:
                self.stderr.write(
                    f"Error tagging job #{entry.job_listing_id}: {e}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: {backfilled_count} jobs newly tagged as LIKED, {already_liked_count} were already liked (Total: {total})."
            )
        )
