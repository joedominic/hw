"""
Preference vectors for job focus ranking: mean of liked job embeddings.
Hybrid: pref_title (title-only) and pref_role_sentences (sentence-level role vectors).
Uses Django cache; invalidate on like/dislike.
"""
import logging
from typing import List, Optional, Tuple

from django.core.cache import cache

from .models import JobListingAction, JobListingEmbedding, JobListing

logger = logging.getLogger(__name__)


def get_preference_vector_cache_key(track: Optional[str] = None) -> str:
    from django.conf import settings
    base = getattr(settings, "PREFERENCE_VECTOR_CACHE_KEY", "job_preference_vector")
    if track:
        return f"{base}:{track}"
    return base


def get_preference_vectors(track: Optional[str] = None) -> Optional[Tuple[List[float], List[float], List[tuple]]]:
    """
    Return (liked_centroid, disliked_centroid, liked_jobs) computed from JobListingEmbedding.

    - liked_centroid: mean vector of all liked job embeddings
    - disliked_centroid: mean vector of all disliked job embeddings (or None)
    - liked_jobs: list of (job_listing_id, title, company_name, embedding, role_vecs?)
      kept for focus breakdown / reasons.

    Returns None if there are no liked embeddings.
    """
    key = get_preference_vector_cache_key(track)
    cached = cache.get(key)
    if cached is not None and isinstance(cached, dict):
        liked = cached.get("liked_centroid")
        disliked = cached.get("disliked_centroid")
        liked_jobs = cached.get("liked_jobs") or []
        if liked is not None:
            return liked, disliked, liked_jobs

    from . import embeddings as embedding_module

    liked_qs = JobListingEmbedding.objects.filter(
        embedding_type=JobListingEmbedding.EmbeddingType.LIKED
    ).select_related("job_listing")
    disliked_qs = JobListingEmbedding.objects.filter(
        embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED
    ).select_related("job_listing")
    if track:
        liked_qs = liked_qs.filter(track=track)
        disliked_qs = disliked_qs.filter(track=track)

    liked_vecs: List[List[float]] = []
    disliked_vecs: List[List[float]] = []
    liked_jobs: List[tuple] = []

    for row in liked_qs:
        job = row.job_listing
        emb = list(row.embedding) if getattr(row, "embedding", None) else None
        if not emb:
            continue
        liked_vecs.append(emb)
        liked_jobs.append((job.id, job.title or "", job.company_name or "", emb, None))

    for row in disliked_qs:
        job = row.job_listing
        emb = list(row.embedding) if getattr(row, "embedding", None) else None
        if not emb:
            continue
        disliked_vecs.append(emb)
    logger.warning(
        "[preference] computing preference vectors (cache miss, track=%s): liked_rows=%d, disliked_rows=%d",
        track or "ic",
        len(liked_vecs),
        len(disliked_vecs),
    )
    if not liked_vecs:
        return None

    liked_centroid = embedding_module.mean_vector(liked_vecs)
    disliked_centroid = embedding_module.mean_vector(disliked_vecs) if disliked_vecs else None

    cache.set(
        key,
        {
            "liked_centroid": liked_centroid,
            "disliked_centroid": disliked_centroid,
            "liked_jobs": liked_jobs,
        },
        timeout=60 * 60 * 24,
    )
    return liked_centroid, disliked_centroid, liked_jobs


def get_preference_vector(track: Optional[str] = None) -> Optional[List[float]]:
    """
    Backwards-compatible helper: return liked_centroid only.
    """
    pv = get_preference_vectors(track=track)
    if pv is None:
        return None
    return pv[0]


def get_liked_jobs_for_focus_reason(track: Optional[str] = None):
    """
    Return liked jobs with embeddings for focus explanations.

    Shape: list of (job_listing_id, title, company_name, embedding, role_vecs?)
    """
    pv = get_preference_vectors(track=track)
    if pv is None or len(pv) < 3:
        return []
    return pv[2]


def invalidate_preference_cache() -> None:
    """Call when user likes or dislikes a job."""
    key = get_preference_vector_cache_key()
    cache.delete(key)


def get_disliked_embeddings_cache_key(track: Optional[str] = None) -> str:
    from django.conf import settings
    base = getattr(settings, "DISLIKED_EMBEDDINGS_CACHE_KEY", "job_disliked_embeddings")
    if track:
        return f"{base}:{track}"
    return base


def get_disliked_embeddings(track: Optional[str] = None) -> List[Tuple[int, List[float]]]:
    """
    Return list of (job_listing_id, embedding) for all disliked jobs.
    Cached; invalidate on like/dislike.
    """
    key = get_disliked_embeddings_cache_key(track)
    cached = cache.get(key)
    if cached is not None and isinstance(cached, list):
        return cached
    qs = JobListingEmbedding.objects.filter(
        embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED
    ).select_related("job_listing")
    if track:
        qs = qs.filter(track=track)
    out: List[Tuple[int, List[float]]] = []
    for row in qs:
        job = row.job_listing
        emb = list(row.embedding) if getattr(row, "embedding", None) else None
        if not emb:
            continue
        out.append((job.id, emb))
    cache.set(key, out, timeout=60 * 60 * 24)
    return out


def invalidate_disliked_embeddings_cache() -> None:
    """Call when user likes or dislikes a job."""
    cache.delete(get_disliked_embeddings_cache_key("ic"))
    cache.delete(get_disliked_embeddings_cache_key("mgmt"))


def get_liked_jobs_with_embeddings(track: Optional[str] = None):
    """
    Backwards-compatible helper: return liked_jobs list used by focus reasoning.
    """
    return get_liked_jobs_for_focus_reason(track=track)
