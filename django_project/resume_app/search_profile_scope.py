"""Resolve search profiles by slug and dual-write legacy track fields."""
from __future__ import annotations

from typing import Optional

from django.contrib.auth import get_user_model
from django.utils.text import slugify

from .models import PipelineEntry, SearchProfile, Track

User = get_user_model()


def unique_profile_slug(user, base: str, *, exclude_slug: str = "") -> str:
    """Return a unique SearchProfile.slug for this user (max 32 chars)."""
    root = (slugify(base) or "search")[:28]
    used = set(SearchProfile.objects.for_user(user).values_list("slug", flat=True))
    if exclude_slug:
        used.discard(exclude_slug)
    slug = root
    n = 2
    while slug in used:
        suffix = f"-{n}"
        slug = f"{root[: 32 - len(suffix)]}{suffix}"
        n += 1
    return slug


def resolve_profile_slug(
    user,
    name: str,
    *,
    explicit_slug: str = "",
    current_slug: str = "",
) -> str:
    """
    Slug for a search profile. Power users may pin an existing non-default slug;
    otherwise each named profile gets its own slug (no standalone Track row).
    """
    default_slug = Track.get_default_slug(user)
    explicit = (explicit_slug or "").strip().lower()
    if explicit and explicit != default_slug:
        if SearchProfile.objects.for_user(user).filter(slug=explicit).exists():
            return explicit
        if Track.objects.for_user(user).filter(slug=explicit).exists():
            return explicit

    current = (current_slug or "").strip().lower()
    if current and current != default_slug:
        sp = SearchProfile.objects.for_user(user).filter(slug=current).first()
        if sp and sp.name == name:
            return current

    return unique_profile_slug(user, name, exclude_slug=current if current != default_slug else "")


def get_search_profile_by_slug(user, slug: Optional[str]) -> Optional[SearchProfile]:
    s = (slug or "").strip().lower()
    if not s:
        return None
    return SearchProfile.objects.for_user(user).filter(slug=s).first()


def available_profile_slugs(user) -> set[str]:
    """Union of legacy Track slugs and SearchProfile slugs for URL/session validation."""
    slugs = set(Track.ensure_baseline(user).values_list("slug", flat=True))
    slugs.update(SearchProfile.objects.for_user(user).values_list("slug", flat=True))
    return slugs


def resolve_active_profile_slug(
    user,
    *,
    profile: Optional[str] = None,
    track: Optional[str] = None,
    session_profile: Optional[str] = None,
    session_track: Optional[str] = None,
) -> str:
    """URL/session resolution: profile= preferred, track= alias, then default track."""
    for raw in (profile, track, session_profile, session_track):
        s = (raw or "").strip().lower()
        if s:
            return s
    return Track.get_default_slug(user)


def dual_write_track_fields(
    *,
    user,
    slug: str,
    search_profile: Optional[SearchProfile] = None,
) -> dict:
    """
    Return kwargs for models that still carry track + search_profile_id during Phase 1.
    """
    norm = (slug or "").strip().lower() or Track.get_default_slug(user)
    sp = search_profile or get_search_profile_by_slug(user, norm)
    return {"track": norm, "search_profile": sp}


def upsert_pipeline_entry(
    user,
    *,
    job_listing_id: int,
    slug: str,
    search_profile: Optional[SearchProfile] = None,
    defaults: Optional[dict] = None,
) -> tuple[PipelineEntry, bool]:
    """Create or fetch a pipeline row with dual-written track + search_profile."""
    dw = dual_write_track_fields(user=user, slug=slug, search_profile=search_profile)
    merged = dict(defaults or {})
    merged.setdefault("search_profile", dw["search_profile"])
    return PipelineEntry.objects.get_or_create(
        owner=user,
        job_listing_id=job_listing_id,
        track=dw["track"],
        defaults=merged,
    )


def ensure_pipeline_entry_on_manual_save(
    user,
    *,
    job_listing_id: int,
    slug: str,
) -> tuple[PipelineEntry, bool]:
    """
    Ensure a PipelineEntry exists after a Find-jobs / Run-search Save.

    - No row (or soft-deleted): land in VETTING (Review) — user already reviewed the listing.
    - Active row: leave stage unchanged (e.g. Huey already placed it in New).

    Returns (entry, landed_in_review) where landed_in_review is True when the row
    was newly created or restored into Review.
    """
    dw = dual_write_track_fields(user=user, slug=slug)
    track = dw["track"]
    entry = (
        PipelineEntry.objects.for_user(user)
        .filter(job_listing_id=job_listing_id, track=track)
        .first()
    )
    if entry is None:
        entry, _created = upsert_pipeline_entry(
            user,
            job_listing_id=job_listing_id,
            slug=track,
            search_profile=dw["search_profile"],
            defaults={"stage": PipelineEntry.Stage.VETTING},
        )
        return entry, True

    if entry.removed_at is not None:
        entry.removed_at = None
        entry.stage = PipelineEntry.Stage.VETTING
        if dw["search_profile"] is not None and entry.search_profile_id is None:
            entry.search_profile = dw["search_profile"]
            entry.save(update_fields=["removed_at", "stage", "search_profile"])
        else:
            entry.save(update_fields=["removed_at", "stage"])
        return entry, True

    return entry, False


def remove_pipeline_entry_on_unsave(
    user,
    *,
    job_listing_id: int,
    slug: str,
) -> int:
    """Soft-delete active pipeline rows for this job + profile on Unsave. Returns count removed."""
    track = (slug or "").strip().lower() or Track.get_default_slug(user)
    entries = list(
        PipelineEntry.objects.for_user(user).filter(
            job_listing_id=job_listing_id,
            track=track,
            removed_at__isnull=True,
        )
    )
    for entry in entries:
        entry.mark_deleted(save=True)
    return len(entries)
