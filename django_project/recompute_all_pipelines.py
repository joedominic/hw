import os
import sys
import time

# Set up Django environment
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
import django
django.setup()

from django.utils import timezone
from django.db.models import Min, Max, Avg, Count
from resume_app.models import JobListing, JobListingTrackMetrics, SearchProfile, PipelineEntry
from resume_app.job_ranking import rank_jobs_by_preference
from resume_app.track_actions import normalize_track_slug


def recompute_track_pipeline(user, track_slug: str, batch_size: int = 64):
    print(f"\n" + "=" * 70)
    print(f" Processing Track: '{track_slug}' for User: '{user.username}'")
    print(f"=" * 70)

    # 1. Fetch metrics for this track
    metrics_qs = JobListingTrackMetrics.objects.filter(
        owner=user, track=track_slug
    ).select_related("job_listing")
    
    total_metrics = metrics_qs.count()
    if total_metrics == 0:
        print(f"No JobListingTrackMetrics found for track '{track_slug}'.")
        return

    # Before stats
    before_stats = metrics_qs.aggregate(
        min_p=Min("focus_percent"), max_p=Max("focus_percent"), avg_p=Avg("focus_percent")
    )
    print(f"Total metrics to re-score: {total_metrics}")
    print(f"Before V1: Min={before_stats['min_p']}%, Max={before_stats['max_p']}%, Avg={before_stats['avg_p']:.1f}%")

    # Fetch all job listings for this track
    all_metrics = list(metrics_qs)
    start_time = time.time()
    updated_count = 0

    # Process in batches
    for i in range(0, len(all_metrics), batch_size):
        batch_metrics = all_metrics[i : i + batch_size]
        batch_jobs = [m.job_listing for m in batch_metrics if m.job_listing]
        
        # Call the updated rank_jobs_by_preference (now using V1 Hybrid Scoring)
        result = rank_jobs_by_preference(batch_jobs, user=user, track=track_slug)
        if result is None:
            print(f"  Warning: rank_jobs_by_preference returned None for batch {i//batch_size + 1}")
            continue

        scores, _, _ = result
        updates = []
        for m, score in zip(batch_metrics, scores):
            if score == -1.0:
                continue
            # Score is in [-1.0, 1.0], mapped back to percent [0, 100]
            v1_pct = int(round(((score + 1.0) / 2.0) * 100))
            m.focus_score = round(score, 4)
            m.focus_percent = v1_pct
            # If focus_after_penalty was present, discount proportionally
            if m.focus_after_penalty is not None:
                penalty_discount = max(0, (before_stats['avg_p'] or 76) - v1_pct)
                m.focus_after_penalty = max(0, v1_pct - 10)
            m.last_scored_at = timezone.now()
            updates.append(m)

        if updates:
            JobListingTrackMetrics.objects.bulk_update(
                updates, ["focus_percent", "focus_after_penalty", "last_scored_at"]
            )
            updated_count += len(updates)

        elapsed = time.time() - start_time
        pct_done = min(100, int((i + len(batch_metrics)) / len(all_metrics) * 100))
        print(f"  Batch {i//batch_size + 1}/{(len(all_metrics) + batch_size - 1)//batch_size}: {updated_count}/{len(all_metrics)} updated ({pct_done}%) - {elapsed:.1f}s", end="\r")

    print(f"\nFinished updating {updated_count} rows in {time.time() - start_time:.1f}s.")

    # After stats
    after_stats = JobListingTrackMetrics.objects.filter(
        owner=user, track=track_slug
    ).aggregate(
        min_p=Min("focus_percent"), max_p=Max("focus_percent"), avg_p=Avg("focus_percent")
    )
    print(f"After V1:  Min={after_stats['min_p']}%, Max={after_stats['max_p']}%, Avg={after_stats['avg_p']:.1f}%")

    # Specifically check Job 76750 if present
    m_intern = JobListingTrackMetrics.objects.filter(
        owner=user, track=track_slug, job_listing_id=76750
    ).first()
    if m_intern:
        print(f"--> Job 76750 (Software Intern) is now: {m_intern.focus_percent}% (focus_after_penalty={m_intern.focus_after_penalty})")


def main():
    print("================================================================================")
    print(" RECOMPUTING ALL PIPELINES WITH V1 HYBRID SCORING")
    print("================================================================================")

    # 1. Fetch active SearchProfiles (run joedhunt first)
    profiles = SearchProfile.objects.all().select_related("owner").order_by("-owner__username", "slug")
    print(f"Found {profiles.count()} Search Profiles across all users.")

    for p in profiles:
        user = p.owner
        slug = p.slug
        recompute_track_pipeline(user, slug)

    print("\n================================================================================")
    print(" ALL PIPELINES RECOMPUTED SUCCESSFULLY WITH V1 HYBRID SCORING!")
    print("================================================================================")


if __name__ == "__main__":
    main()
