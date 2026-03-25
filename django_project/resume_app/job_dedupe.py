"""
Cluster pipeline rows that are the same job (same title, company, description body)
posted at different locations/URLs, and soft-delete duplicates per track.

Fingerprint ignores location and job URL so multi-location postings collapse to one row.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from django.db import models

from .models import JobListingTrackMetrics, PipelineEntry, Track

logger = logging.getLogger(__name__)

# Hash enough of the description to distinguish roles while staying stable for identical postings.
_DESCRIPTION_PREFIX_CHARS = 2000


def _normalize_ws(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    return " ".join(text.lower().split())


def job_listing_fingerprint(job) -> str:
    """Stable key: title + company + hashed description prefix (location/url excluded)."""
    t = _normalize_ws(job.title or "")
    c = _normalize_ws(job.company_name or "")
    desc = _normalize_ws((job.description or "")[:_DESCRIPTION_PREFIX_CHARS])
    raw = f"{t}|{c}|{desc}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return h


def stage_filter_q(stage: str, *, include_done: bool) -> models.Q:
    """
    Build a Q object for PipelineEntry.stage.

    `stage` is one of: "all", "pipeline", "vetting", "applying", "done".
    Legacy blank stage counts as pipeline.
    """
    s = (stage or "all").strip().lower()
    if s in ("all", "*"):
        q = (
            models.Q(stage=PipelineEntry.Stage.PIPELINE)
            | models.Q(stage=PipelineEntry.Stage.VETTING)
            | models.Q(stage=PipelineEntry.Stage.APPLYING)
            | models.Q(stage="")
        )
        if include_done:
            q |= models.Q(stage=PipelineEntry.Stage.DONE)
        return q
    if s == "pipeline":
        return models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)
    if s == "vetting":
        return models.Q(stage=PipelineEntry.Stage.VETTING)
    if s == "applying":
        return models.Q(stage=PipelineEntry.Stage.APPLYING)
    if s == "done":
        return models.Q(stage=PipelineEntry.Stage.DONE)
    raise ValueError(f"Invalid stage: {stage!r}")


def _winner_sort_key(
    entry: PipelineEntry,
    metrics_by_job_id: dict[int, JobListingTrackMetrics],
) -> tuple:
    m = metrics_by_job_id.get(entry.job_listing_id)
    fa = m.focus_after_penalty if m and m.focus_after_penalty is not None else -1
    pm = m.preference_margin if m and m.preference_margin is not None else -1
    fp = m.focus_percent if m and m.focus_percent is not None else -1
    ts = entry.added_at.timestamp() if entry.added_at else 0.0
    # Prefer lower job_listing_id when scores tie (stable).
    return (fa, pm, fp, ts, -entry.job_listing_id)


def dedupe_pipeline_entries(
    *,
    track_slug: str | None,
    stage: str,
    include_done: bool,
) -> dict[str, object]:
    """
    Soft-delete duplicate PipelineEntry rows within each fingerprint group.

    - Clustering is per track (never merges across tracks).
    - `track_slug`: None or '*' / 'all' → every known track (Track table).
    - `stage`: 'all' or '*' for all stages (see include_done for Done).
    Returns summary dict with counts.
    """
    stage_norm = (stage or "all").strip().lower()
    st_q = stage_filter_q(stage_norm, include_done=include_done)

    if track_slug and str(track_slug).strip().lower() not in ("*", "all", ""):
        tracks = [str(track_slug).strip().lower()]
    else:
        tracks = list(Track.ensure_baseline().values_list("slug", flat=True))

    total_removed = 0
    total_groups = 0
    per_track: dict[str, dict[str, int]] = {}

    for tslug in tracks:
        base = (
            PipelineEntry.objects.filter(track=tslug, removed_at__isnull=True)
            .filter(st_q)
            .select_related("job_listing")
        )
        entries = list(base)
        if not entries:
            per_track[tslug] = {"removed": 0, "duplicate_groups": 0}
            continue

        job_ids = {e.job_listing_id for e in entries}
        metrics_list = JobListingTrackMetrics.objects.filter(
            track=tslug,
            job_listing_id__in=job_ids,
        )
        metrics_by_job_id = {m.job_listing_id: m for m in metrics_list}

        fingerprint_map: dict[str, list[PipelineEntry]] = defaultdict(list)
        for e in entries:
            fp = job_listing_fingerprint(e.job_listing)
            fingerprint_map[fp].append(e)

        removed_here = 0
        groups_here = 0
        for _fp, group in fingerprint_map.items():
            if len(group) < 2:
                continue
            groups_here += 1
            winner = max(group, key=lambda ent: _winner_sort_key(ent, metrics_by_job_id))
            for ent in group:
                if ent.id == winner.id:
                    continue
                ent.mark_deleted(save=True)
                removed_here += 1

        total_removed += removed_here
        total_groups += groups_here
        per_track[tslug] = {"removed": removed_here, "duplicate_groups": groups_here}
        if removed_here:
            logger.info(
                "[dedupe_pipeline_entries] track=%s removed=%d duplicate_groups=%d",
                tslug,
                removed_here,
                groups_here,
            )

    return {
        "status": "success",
        "tracks_processed": len(tracks),
        "duplicate_groups": total_groups,
        "entries_removed": total_removed,
        "per_track": per_track,
    }
