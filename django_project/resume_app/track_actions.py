"""
Track-scoped job listing actions (likes, dislikes, saves).

Semantics:
- Rows with track="" are legacy "global" actions from before per-track storage.
  They apply to every search/pipeline context (same as the plan's global hide for old data).
- Rows with a concrete slug apply only when that slug is the active track.
- Preference centroids (liked/disliked embeddings) fold legacy rows into the *default*
  track only, so old likes/dislikes still shape the default-track model without
  polluting other tracks.
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
    """Dislikes that exclude a job from search results for this track context."""
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
    slug = normalize_track_slug(track, user)
    return set(
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.DISLIKED)
        .filter(q_disliked_rows_for_search(slug))
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
