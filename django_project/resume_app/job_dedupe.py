"""
Cluster pipeline rows that are the same job (same title, company, description body)
posted at different locations/URLs, and soft-delete duplicates per track.

Fingerprint ignores location and job URL so multi-location postings collapse to one row.

All operations are **owner-scoped**. Pass the owning ``user`` so that entries from
one tenant can never be deleted by a different tenant's dedupe run.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.db import models

from .models import JobListingTrackMetrics, PipelineEntry, Track

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by",
    "can", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him", "himself",
    "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me", "more",
    "most", "my", "myself", "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should", "so", "some", "such",
    "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "with", "would", "you", "your",
    "yours", "yourself", "yourselves",
}

_COMPANY_NOISE_WORDS = {
    "bank", "na", "n", "a", "national", "association", "inc", "incorporated", "llc", "llp",
    "ltd", "limited", "corp", "corporation", "co", "c", "o", "company", "group", "holdings",
    "services", "technologies", "technology", "solutions", "international", "usa",
    "us", "america", "the", "and",
}

_HEADER_WORDS = {
    "job", "description", "position", "role", "about", "us", "the", "summary",
    "overview", "responsibilities", "duties",
}


def canonical_company(name: str | None) -> str:
    """Normalize company name across boards (strips corporate suffixes, punctuation, whitespace)."""
    if not name or not isinstance(name, str):
        return ""
    # Strip location suffix if attached e.g. "Capital One ·Plano, TX"
    text = name.split("·")[0]
    # Remove dots within acronyms e.g. "N.A." -> "NA", "J.P." -> "JP"
    text = re.sub(r"\.(?!\s)", "", text)
    # Replace non-alphanumeric with spaces
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).lower()
    words = [w for w in text.split() if w not in _COMPANY_NOISE_WORDS]
    if not words:
        # Fallback if entire name was filtered
        words = text.split()
    return "".join(words)


def canonical_title(title: str | None) -> str:
    """Normalize job title (strips punctuation, common brackets, whitespace)."""
    if not title or not isinstance(title, str):
        return ""
    text = re.sub(r"[^a-zA-Z0-9]+", " ", title).lower()
    return "".join(text.split())


def canonical_description_prefix(desc: str | None, max_tokens: int = 30) -> str:
    """Extract significant keywords from job description prefix, ignoring boilerplate headers."""
    if not desc or not isinstance(desc, str):
        return ""
    text = re.sub(r"[^a-zA-Z0-9]+", " ", desc).lower()
    words = text.split()
    # Strip leading header words (e.g. "job description", "about the job")
    start_idx = 0
    while start_idx < len(words) and words[start_idx] in _HEADER_WORDS:
        start_idx += 1
    significant = [w for w in words[start_idx:] if w not in _STOPWORDS and w not in _HEADER_WORDS and len(w) > 1]
    return " ".join(significant[:max_tokens])


def _normalize_ws(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    return " ".join(text.lower().split())


def job_listing_fingerprint(job) -> str:
    """
    Cross-board stable key: canonical company + canonical title + normalized description tokens.
    Collapses cross-board postings (LinkedIn, Indeed, Dice) into a unified deduplication cluster.
    """
    c = canonical_company(getattr(job, "company_name", None))
    t = canonical_title(getattr(job, "title", None))
    d = canonical_description_prefix(getattr(job, "description", None))
    raw = f"{c}|{t}|{d}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return h


def title_company_key(title: str | None, company: str | None) -> tuple[str, str]:
    """Normalized (title, company) key for Find-jobs result de-dupe."""
    return (canonical_title(title), canonical_company(company))


def dedupe_payloads_by_title_company(payloads: list, dropped_details: Optional[dict] = None) -> list:
    """
    Collapse multi-location / cross-board duplicates in search results.

    Keeps the first payload for each normalized title+company pair (call after
    ranking so the best-fit copy wins). Rows with both title and company empty
    are kept as-is.
    """
    if not payloads:
        return payloads
    seen: set[tuple[str, str]] = set()
    canonical_info: dict[tuple[str, str], tuple[str, str]] = {}
    out: list = []
    for payload in payloads:
        title = getattr(payload, "title", None)
        company = getattr(payload, "company_name", None)
        pid = getattr(payload, "id", None)
        if isinstance(payload, dict):
            title = payload.get("title")
            company = payload.get("company_name")
            pid = payload.get("id")
        key = title_company_key(title, company)
        if not key[0] and not key[1]:
            out.append(payload)
            continue
        if key in seen:
            if dropped_details is not None and pid is not None:
                orig_title, orig_company = canonical_info.get(key, (title or "", company or ""))
                dropped_details[pid] = {
                    "disposition": "eliminated_duplicate",
                    "reason": f"Eliminated: Duplicate posting of '{orig_title}' at {orig_company}",
                }
            continue
        seen.add(key)
        canonical_info[key] = (title or "", company or "")
        out.append(payload)
    return out



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
    metrics_map: dict[tuple[str, int], JobListingTrackMetrics],
) -> tuple:
    """
    Rank duplicate pipeline entries across search profiles.
    - Prioritizes active workflow progression (Applying > Vetting > Pipeline)
    - Prioritizes highest match or fit % (focus_after_penalty, focus_percent, vetting_interview_probability)
    - Prioritizes preference margin
    - Tie-breaker: recency and stable entry id.
    """
    stage_priority = {
        PipelineEntry.Stage.APPLYING: 3,
        PipelineEntry.Stage.VETTING: 2,
        PipelineEntry.Stage.PIPELINE: 1,
        "": 1,
    }
    st_val = stage_priority.get(entry.stage, 0)

    m = metrics_map.get((entry.track, entry.job_listing_id))
    fa = m.focus_after_penalty if (m and m.focus_after_penalty is not None) else -1
    fp = m.focus_percent if (m and m.focus_percent is not None) else -1
    best_fit = max(fa, fp)

    vetting_prob = entry.vetting_interview_probability if entry.vetting_interview_probability is not None else -1
    pm = m.preference_margin if (m and m.preference_margin is not None) else -999
    ts = entry.added_at.timestamp() if entry.added_at else 0.0

    return (st_val, best_fit, vetting_prob, pm, ts, -entry.id)


def dedupe_pipeline_entries(
    *,
    user,
    track_slug: str | None = "*",
    stage: str = "all",
    include_done: bool = False,
) -> dict[str, object]:
    """
    De-dupe jobs across search profiles for *user* (excluding Applied / Done stage by default).

    When duplicate copies of a job are found across search profiles or within a profile:
    - Keeps the job in the profile with the highest fit or match %.
    - Soft-deletes duplicate entries in other profiles for that tenant.
    - Excludes Applied (Stage.DONE) entries from deletion.

    - `track_slug`: None or '*' / 'all' → merges/dedupes across ALL search profiles for this user.
                   Specific slug → scopes dedupe to that single track.
    - `stage`: 'all' or '*' for all stages (see include_done for Done).
    Returns summary dict with counts.
    """
    stage_norm = (stage or "all").strip().lower()
    st_q = stage_filter_q(stage_norm, include_done=include_done)

    is_cross_profile = not track_slug or str(track_slug).strip().lower() in ("*", "all", "")

    if is_cross_profile:
        tracks = list(Track.ensure_baseline(user).values_list("slug", flat=True))
        base = (
            PipelineEntry.objects.for_user(user)
            .filter(removed_at__isnull=True)
            .filter(st_q)
            .select_related("job_listing")
        )
    else:
        tslug = str(track_slug).strip().lower()
        tracks = [tslug]
        base = (
            PipelineEntry.objects.for_user(user)
            .filter(track=tslug, removed_at__isnull=True)
            .filter(st_q)
            .select_related("job_listing")
        )

    entries = list(base)
    if not entries:
        return {
            "status": "success",
            "tracks_processed": len(tracks),
            "duplicate_groups": 0,
            "entries_removed": 0,
            "per_track": {t: {"removed": 0, "duplicate_groups": 0} for t in tracks},
        }

    job_ids = {e.job_listing_id for e in entries}
    metrics_list = JobListingTrackMetrics.objects.for_user(user).filter(
        job_listing_id__in=job_ids,
    )
    metrics_map = {(m.track, m.job_listing_id): m for m in metrics_list}

    fingerprint_map: dict[str, list[PipelineEntry]] = defaultdict(list)
    for e in entries:
        fp = job_listing_fingerprint(e.job_listing)
        fingerprint_map[fp].append(e)

    total_removed = 0
    total_groups = 0
    per_track: dict[str, dict[str, int]] = {t: {"removed": 0, "duplicate_groups": 0} for t in tracks}

    for _fp, group in fingerprint_map.items():
        if len(group) < 2:
            continue
        total_groups += 1
        winner = max(group, key=lambda ent: _winner_sort_key(ent, metrics_map))

        for ent in group:
            if ent.id == winner.id:
                continue
            # Never delete an entry if it is in Applied (DONE) stage
            if ent.stage == PipelineEntry.Stage.DONE:
                continue

            ent.mark_deleted(save=True)
            total_removed += 1
            if ent.track in per_track:
                per_track[ent.track]["removed"] += 1
            else:
                per_track[ent.track] = {"removed": 1, "duplicate_groups": 0}

    if total_removed:
        logger.info(
            "[dedupe_pipeline_entries] user=%s cross_profile=%s removed=%d duplicate_groups=%d",
            getattr(user, "id", user),
            is_cross_profile,
            total_removed,
            total_groups,
        )

    return {
        "status": "success",
        "tracks_processed": len(tracks),
        "duplicate_groups": total_groups,
        "entries_removed": total_removed,
        "per_track": per_track,
    }


def dedupe_pipeline_entries_all_users(
    *,
    track_slug: str | None = "*",
    stage: str = "all",
    include_done: bool = False,
) -> dict[str, object]:
    """
    Staff / admin helper: run ``dedupe_pipeline_entries`` for every active user.

    Never called from user-facing flows. Used by the Huey admin task
    (``dedupe_pipeline_jobs_task``) and the management command.
    """
    User = get_user_model()
    combined: dict[str, object] = {
        "status": "success",
        "tracks_processed": 0,
        "duplicate_groups": 0,
        "entries_removed": 0,
        "per_user": {},
    }
    for user in User.objects.filter(is_active=True):
        try:
            result = dedupe_pipeline_entries(
                user=user,
                track_slug=track_slug,
                stage=stage,
                include_done=include_done,
            )
            combined["tracks_processed"] = int(combined["tracks_processed"]) + int(result.get("tracks_processed") or 0)
            combined["duplicate_groups"] = int(combined["duplicate_groups"]) + int(result.get("duplicate_groups") or 0)
            combined["entries_removed"] = int(combined["entries_removed"]) + int(result.get("entries_removed") or 0)
            combined["per_user"][str(user.id)] = result  # type: ignore[index]
        except Exception as e:
            logger.exception("[dedupe_all_users] failed user=%s: %s", user.id, e)
    return combined
