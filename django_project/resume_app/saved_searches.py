"""Helpers for SearchProfile (saved search presets)."""
from __future__ import annotations

from urllib.parse import urlencode

from django.db import models
from django.http import HttpRequest
from django.urls import reverse

from .job_search_core import normalize_site_names
from .models import PipelineEntry, SearchProfile, Track, UserResume
from .search_profile_scope import resolve_profile_slug


def backfill_search_profile_slugs(user) -> None:
    """Assign slugs to legacy profiles still missing slug or on the default track."""
    default_slug = Track.get_default_slug(user)
    for saved in SearchProfile.objects.for_user(user).filter(
        models.Q(slug="") | models.Q(profile_slug="") | models.Q(profile_slug=default_slug)
    ):
        new_slug = resolve_profile_slug(
            user,
            saved.name,
            current_slug=saved.slug or saved.profile_slug,
        )
        if new_slug != saved.slug:
            saved.slug = new_slug
            saved.profile_slug = new_slug
            saved.save(update_fields=["slug", "profile_slug"])


def list_saved_searches(user):
    backfill_search_profile_slugs(user)
    return list(SearchProfile.objects.for_user(user).order_by("-updated_at", "name"))


def saved_search_profile_tabs(user) -> list[dict[str, str]]:
    """
    One My Jobs / Find Jobs profile chip per search profile (same order as Find jobs sidebar).
    """
    backfill_search_profile_slugs(user)
    tabs: list[dict[str, str]] = []
    seen: set[str] = set()
    for saved in SearchProfile.objects.for_user(user).order_by("-updated_at", "name"):
        slug = (saved.slug or saved.profile_slug or "").strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        tabs.append({"slug": slug, "label": saved.name})
    return tabs


def pipeline_track_tabs_for_board(user, *, power_user: bool) -> list[dict[str, str]]:
    """Search profile tabs for the pipeline board."""
    if power_user:
        return [
            {"slug": t.slug, "label": t.label or t.slug}
            for t in Track.ensure_baseline(user)
        ]
    return saved_search_profile_tabs(user)


def get_saved_search_or_none(user, preset_id: int | str | None) -> SearchProfile | None:
    if not preset_id:
        return None
    try:
        pid = int(preset_id)
    except (TypeError, ValueError):
        return None
    return SearchProfile.objects.for_user(user).filter(pk=pid).first()


def apply_saved_search_to_get(saved: SearchProfile) -> dict:
    """Return dict of form values to override on job search GET."""
    slug = (saved.slug or saved.profile_slug or "").strip()
    return {
        "query": (saved.search_term or "").strip(),
        "location": (saved.location or "").strip(),
        "profile_slug": slug,
        "resume_id_val": saved.resume_id,
        "min_score_raw": str(saved.min_score) if saved.min_score is not None else "",
        "results_wanted_val": saved.results_wanted or 50,
        "selected_site_names": normalize_site_names(
            saved.site_names if isinstance(saved.site_names, list) else None
        ),
        "llm_model": (saved.llm_model or "").strip(),
    }


def create_or_update_saved_search(
    user,
    *,
    name: str,
    search_term: str = "",
    location: str = "",
    profile_slug: str = "",
    resume_id: int | None = None,
    min_score: int | None = None,
    results_wanted: int = 50,
    site_names: list[str] | None = None,
    llm_model: str = "",
    preset_id: int | None = None,
) -> SearchProfile:
    name = (name or "").strip()
    term = (search_term or "").strip()
    if not term:
        raise ValueError("Search term is required.")

    resume = None
    if resume_id:
        resume = UserResume.objects.for_user(user).filter(is_library=True, id=resume_id).first()
        if not resume:
            raise ValueError("Invalid resume selection.")

    sites = normalize_site_names(site_names)
    if preset_id:
        saved = SearchProfile.objects.for_user(user).filter(pk=preset_id).first()
        if not saved:
            raise ValueError("Saved search not found.")
        if not name:
            name = (saved.name or "").strip()
        if not name:
            raise ValueError("Name is required.")
        if len(name) > 255:
            raise ValueError("Name is too long.")
        conflict = (
            SearchProfile.objects.for_user(user)
            .filter(name=name)
            .exclude(pk=saved.pk)
            .exists()
        )
        if conflict:
            raise ValueError(f'A saved search named "{name}" already exists.')
        default_slug = Track.get_default_slug(user)
        explicit = (profile_slug or "").strip().lower()
        existing_slug = (saved.slug or saved.profile_slug or "").strip().lower()
        if (
            existing_slug
            and existing_slug != default_slug
            and not (explicit and explicit != default_slug)
        ):
            slug = existing_slug
        elif saved.name != name:
            slug = resolve_profile_slug(
                user,
                name,
                explicit_slug=explicit,
                current_slug=existing_slug,
            )
        else:
            slug = existing_slug or resolve_profile_slug(user, name)
    else:
        if not name:
            raise ValueError("Name is required.")
        if len(name) > 255:
            raise ValueError("Name is too long.")
        saved = SearchProfile(owner=user)
        if SearchProfile.objects.for_user(user).filter(name=name).exists():
            raise ValueError(f'A saved search named "{name}" already exists.')
        slug = resolve_profile_slug(user, name)

    saved.name = name
    saved.slug = slug
    saved.search_term = term[:512]
    saved.location = (location or "").strip()[:512]
    saved.profile_slug = slug
    saved.resume = resume
    saved.min_score = min_score
    saved.results_wanted = max(10, min(200, int(results_wanted or 50)))
    saved.site_names = sites
    saved.llm_model = (llm_model or "").strip()[:128]
    saved.save()
    from .models import JobSearchTask
    from .saved_search_schedule import sync_job_search_task_from_saved

    task = JobSearchTask.objects.for_user(user).filter(saved_search=saved).first()
    if task:
        sync_job_search_task_from_saved(saved, task)
        task.save(
            update_fields=[
                "name",
                "search_term",
                "location",
                "track",
                "jobs_to_fetch",
                "site_name",
                "updated_at",
            ]
        )
    return saved


def delete_saved_search(user, preset_id: int) -> None:
    saved = SearchProfile.objects.for_user(user).filter(pk=preset_id).first()
    if not saved:
        raise ValueError("Saved search not found.")
    slug = (saved.slug or saved.profile_slug or "").strip().lower()
    default_slug = Track.get_default_slug(user)
    saved.delete()
    if slug and slug != default_slug:
        still_used = SearchProfile.objects.for_user(user).filter(
            models.Q(slug=slug) | models.Q(profile_slug=slug)
        ).exists()
        has_pipeline = PipelineEntry.objects.for_user(user).filter(
            track=slug,
            removed_at__isnull=True,
        ).exists()
        if not still_used and not has_pipeline:
            Track.objects.for_user(user).filter(slug=slug, is_default=False).delete()


def parse_saved_search_from_request(request: HttpRequest) -> dict:
    """Extract saved-search fields from POST (save form or duplicated search fields)."""
    min_score_raw = (request.POST.get("min_score") or "").strip()
    min_score = None
    if min_score_raw:
        try:
            min_score = int(min_score_raw)
        except ValueError:
            min_score = None
    resume_id_raw = (request.POST.get("resume_id") or "").strip()
    resume_id = None
    if resume_id_raw and resume_id_raw.lower() != "none":
        try:
            resume_id = int(resume_id_raw)
        except ValueError:
            resume_id = None
    results_wanted_raw = (request.POST.get("results_wanted") or "50").strip()
    try:
        results_wanted = int(results_wanted_raw)
    except ValueError:
        results_wanted = 50
    preset_id_raw = (request.POST.get("preset_id") or "").strip()
    preset_id = None
    if preset_id_raw and not (request.POST.get("save_as_new") or "").strip():
        try:
            preset_id = int(preset_id_raw)
        except ValueError:
            preset_id = None
    profile = (
        request.POST.get("profile")
        or request.POST.get("track")
        or request.POST.get("profile_slug")
        or ""
    ).strip()
    return {
        "name": (request.POST.get("saved_search_name") or request.POST.get("name") or "").strip(),
        "search_term": (request.POST.get("q") or request.POST.get("search_term") or "").strip(),
        "location": (request.POST.get("location") or "").strip(),
        "profile_slug": profile,
        "resume_id": resume_id,
        "min_score": min_score,
        "results_wanted": results_wanted,
        "site_names": request.POST.getlist("site_name"),
        "llm_model": (request.POST.get("llm_model") or "").strip(),
        "preset_id": preset_id,
    }


def saved_search_matches_params(
    saved: SearchProfile,
    *,
    search_term: str,
    location: str,
    profile_slug: str,
    resume_id: int | None,
    min_score_raw: str,
    results_wanted: int,
    site_names: list[str],
    llm_model: str | None,
) -> bool:
    """True when the current search form matches a saved search (update vs save-as-new)."""
    parsed_min = None
    if min_score_raw:
        try:
            parsed_min = int(min_score_raw)
        except ValueError:
            pass
    if (saved.search_term or "").strip() != (search_term or "").strip():
        return False
    if (saved.location or "").strip() != (location or "").strip():
        return False
    saved_slug = (saved.slug or saved.profile_slug or "").strip().lower()
    if saved_slug != (profile_slug or "").strip().lower():
        return False
    if saved.resume_id != resume_id:
        return False
    if saved.min_score != parsed_min:
        return False
    if max(10, min(200, int(saved.results_wanted or 50))) != results_wanted:
        return False
    saved_sites = set(
        normalize_site_names(saved.site_names if isinstance(saved.site_names, list) else None)
    )
    if saved_sites != set(site_names or []):
        return False
    if (saved.llm_model or "").strip() != (llm_model or "").strip():
        return False
    return True


def build_jobs_search_url(
    *,
    query: str = "",
    location: str = "",
    profile_slug: str = "",
    resume_id: int | None = None,
    min_score: int | None = None,
    results_wanted: int | None = None,
    site_names: list[str] | None = None,
    llm_model: str = "",
    preset_id: int | None = None,
    view: str = "results",
    from_save: bool = False,
) -> str:
    params: list[tuple[str, str]] = []
    if view and view != "results":
        params.append(("view", view))
    if query:
        params.append(("q", query))
    if location:
        params.append(("location", location))
    if profile_slug:
        params.append(("profile", profile_slug))
        params.append(("track", profile_slug))
    if resume_id:
        params.append(("resume_id", str(resume_id)))
    if min_score is not None:
        params.append(("min_score", str(min_score)))
    if results_wanted is not None:
        params.append(("results_wanted", str(results_wanted)))
    if llm_model:
        params.append(("llm_model", llm_model))
    if preset_id:
        params.append(("preset", str(preset_id)))
    if from_save:
        params.append(("from_save", "1"))
    for site in site_names or []:
        params.append(("site_name", site))
    qs = urlencode(params)
    return reverse("jobs_search") + (f"?{qs}" if qs else "")


# Backward-compatible aliases
resolve_profile_slug_for_saved_search = resolve_profile_slug
backfill_saved_search_profile_tracks = backfill_search_profile_slugs
SavedJobSearch = SearchProfile
