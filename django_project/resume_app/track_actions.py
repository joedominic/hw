"""
Track-scoped job listing actions (likes, dislikes, hides, saves).

Semantics:
- Rows with track="" are legacy "global" actions from before per-track storage.
  They apply to every search/pipeline context (same as the plan's global hide for old data).
- Rows with a concrete slug apply only when that slug is the active track.
- Preference centroids (liked/disliked embeddings) fold legacy rows into the *default*
  track only, so old likes/dislikes still shape the default-track model without
  polluting other tracks.
- ``disliked`` feeds preference modeling and also excludes from search.
- ``hidden`` excludes from search only and does not affect Match Scoring.
"""
from typing import Optional, Set

from django.contrib.auth import get_user_model

from django.db.models import Q

from .models import JobListingAction, Track

User = get_user_model()


def normalize_track_slug(track: Optional[str], user) -> str:
    t = (track or "").strip().lower()
    return t if t else Track.get_default_slug(user)


def q_disliked_rows_for_search(slug: str) -> Q:
    """Track scope for actions that exclude a job from search results."""
    return Q(track=slug) | Q(track="")


def q_saved_rows_for_track(slug: str) -> Q:
    """Saved rows associated with this pipeline / favourites context."""
    return Q(track=slug) | Q(track="")


def q_preference_embedding_track(slug: str, user) -> Q:
    """Embedding rows that contribute to preference vectors for this track."""
    q = Q(track=slug)
    if slug == Track.get_default_slug(user):
        q |= Q(track="")
    return q


def q_clear_on_sentiment_change(slug: str) -> Q:
    """Track scope for clearing opposing actions/embeddings (per-track + legacy global)."""
    return Q(track=slug) | Q(track="")


def disliked_listing_id_set(user, track: Optional[str]) -> Set[int]:
    """Jobs disliked for FIT preference (also excluded from search)."""
    slug = normalize_track_slug(track, user)
    return set(
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.DISLIKED)
        .filter(q_disliked_rows_for_search(slug))
        .values_list("job_listing_id", flat=True)
    )


def hidden_listing_id_set(user, track: Optional[str], cooldown_days: Optional[int] = None) -> Set[int]:
    """Jobs hidden from search only (no FIT impact).

    Respects a cooldown window (defaults to settings.PIPELINE_DELETED_REPOST_COOLDOWN_DAYS or 14 days).
    Hidden actions older than the cooldown window expire and no longer exclude the job from search.
    If cooldown_days is 0 or False, all hidden jobs are returned regardless of age.
    """
    slug = normalize_track_slug(track, user)
    qs = (
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.HIDDEN)
        .filter(q_disliked_rows_for_search(slug))
    )
    if cooldown_days is None:
        from django.conf import settings
        cooldown_days = getattr(settings, "PIPELINE_DELETED_REPOST_COOLDOWN_DAYS", 14)
    if cooldown_days and cooldown_days > 0:
        from datetime import timedelta
        from django.utils import timezone
        cutoff = timezone.now() - timedelta(days=cooldown_days)
        qs = qs.filter(created_at__gte=cutoff)
    return set(qs.values_list("job_listing_id", flat=True))


def excluded_listing_id_set(user, track: Optional[str], cooldown_days: Optional[int] = None) -> Set[int]:
    """Jobs that must not appear in search results (hidden within cooldown ∪ disliked permanently)."""
    return disliked_listing_id_set(user, track) | hidden_listing_id_set(user, track, cooldown_days=cooldown_days)


def liked_listing_id_set(user, track: Optional[str]) -> Set[int]:
    """Liked job IDs for this track (plus legacy global likes)."""
    slug = normalize_track_slug(track, user)
    return set(
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.LIKED)
        .filter(q_saved_rows_for_track(slug))
        .values_list("job_listing_id", flat=True)
    )


def saved_listing_id_set(user, track: Optional[str]) -> Set[int]:
    slug = normalize_track_slug(track, user)
    return set(
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.SAVED)
        .filter(q_saved_rows_for_track(slug))
        .values_list("job_listing_id", flat=True)
    )


def record_job_liked(
    *,
    user,
    job,
    track: Optional[str] = None,
    search_profile=None,
    sync_embedding: bool = False,
) -> JobListingAction:
    """Mark a job as LIKED for the given user and track/search_profile.

    Ensures:
    1. JobListingAction row with ActionType.LIKED is created / updated.
    2. Any opposing actions (DISLIKED, HIDDEN) for this track are removed.
    3. Any opposing DISLIKED embeddings for this track are removed.
    4. Preference and disliked vector caches are invalidated.
    5. The positive LIKED embedding is stored asynchronously (or synchronously if sync_embedding=True).
    """
    from django.db import transaction
    from .models import JobListingAction, JobListingEmbedding
    from .search_profile_scope import dual_write_track_fields
    from .preference import invalidate_preference_cache, invalidate_disliked_embeddings_cache

    raw_track = normalize_track_slug(track, user)
    dw = dual_write_track_fields(user=user, slug=raw_track, search_profile=search_profile)
    with transaction.atomic():
        action_obj, _ = JobListingAction.objects.get_or_create(
            owner=user,
            job_listing=job,
            action=JobListingAction.ActionType.LIKED,
            track=dw["track"],
            defaults={"search_profile": dw["search_profile"]},
        )
        JobListingAction.objects.for_user(user).filter(
            job_listing=job,
            action__in=[
                JobListingAction.ActionType.DISLIKED,
                JobListingAction.ActionType.HIDDEN,
            ],
        ).filter(q_clear_on_sentiment_change(dw["track"])).delete()
        JobListingEmbedding.objects.for_user(user).filter(
            job_listing=job, embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED
        ).filter(q_clear_on_sentiment_change(dw["track"])).delete()

    invalidate_preference_cache(user)
    invalidate_disliked_embeddings_cache(user)

    from . import embeddings as embedding_module
    from .jobs_api import _store_feedback_embedding_async, _store_feedback_embedding_best_effort

    if sync_embedding:
        _store_feedback_embedding_best_effort(
            user=user,
            job=job,
            embedding_type=JobListingEmbedding.EmbeddingType.LIKED,
            track=dw["track"],
            search_profile=dw["search_profile"],
            embed_fn=embedding_module.embed_job_text,
        )
    else:
        _store_feedback_embedding_async(
            user=user,
            job=job,
            embedding_type=JobListingEmbedding.EmbeddingType.LIKED,
            track=dw["track"],
            search_profile=dw["search_profile"],
            embed_fn=embedding_module.embed_job_text,
        )
    return action_obj

