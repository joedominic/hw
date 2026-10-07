"""
Request-agnostic job search: fetch, filter, rank.
Used by jobs_search API and by run_job_search_task (pipeline).
"""
import logging
import re
from typing import List, Optional, Tuple, Dict

from django.conf import settings
from django.utils import timezone

from .models import JobListing, JobListingAction, JobListingTrackMetrics, PipelineEntry
from .track_actions import (
    disliked_listing_id_set,
    excluded_listing_id_set,
    hidden_listing_id_set,
    liked_listing_id_set,
    normalize_track_slug,
    saved_listing_id_set,
)
from .job_sources import fetch_jobs, normalize_site_names, upsert_job_listing_from_fetch, _parse_date_posted
from .schemas import JobPayload
from .preference import (
    get_preference_vectors,
    get_liked_jobs_for_focus_reason,
    get_disliked_embeddings,
    invalidate_preference_cache,
    invalidate_disliked_embeddings_cache,
)
from .job_ranking import gated_combined_score as _gated_combined_score
from .job_ranking import rank_jobs_by_preference
from .disqualifiers import (
    build_disqualifier_pattern,
    job_matches_disqualifiers,
)
from . import embeddings as embedding_module
from .utils import format_job_source_label

logger = logging.getLogger(__name__)

# Keep in sync with tasks.VETTING_MATCHING_JD_MIN_CHARS
VETTING_MATCHING_JD_MIN_CHARS = 2000

# Sources where a short stored description can be replaced via detail fetch (see dice/levels clients).
ENRICHABLE_JD_SOURCES = frozenset({"dice", "levels", "builtin"})


def effective_vetting_job_description(job: JobListing, *, enrich: bool = False) -> str:
    """
    Description used for vetting length checks and interview status.

    When ``enrich`` is True, Dice/Levels/BuiltIn listings below the vetting minimum may
    fetch and persist full job-detail text (same path as Show description).
    """
    current = (job.description or "").strip()
    if not enrich or len(current) >= VETTING_MATCHING_JD_MIN_CHARS:
        return current
    src = (job.source or "").strip().lower()
    if src == "dice":
        from .sourcing.clients.dice_client import enrich_dice_job_listing_description

        return (enrich_dice_job_listing_description(job) or current).strip()
    if src == "levels":
        from .sourcing.clients.levels_client import enrich_levels_job_listing_description

        return (enrich_levels_job_listing_description(job) or current).strip()
    if src == "builtin":
        from .sourcing.clients.builtin_client import enrich_builtin_job_listing_description

        return (enrich_builtin_job_listing_description(job) or current).strip()
    if src == "greenhouse":
        from .sourcing.clients.greenhouse_client import enrich_greenhouse_job_listing_description

        return (enrich_greenhouse_job_listing_description(job) or current).strip()
    return current


def resolve_interview_display_status(
    *,
    description: str,
    interview_probability: Optional[int],
    source: str = "",
) -> Optional[str]:
    """
    When interview_probability is set, returns None (UI shows scored pill).

    Otherwise returns ``short_jd`` or ``pending`` for Review/Applying boards.
    Dice/Levels rows with a short *stored* snippet show pending — full text is
    fetched before vetting matching runs (same as Show description).
    """
    if interview_probability is not None:
        return None
    raw_jd = (description or "").strip()
    if len(raw_jd) >= VETTING_MATCHING_JD_MIN_CHARS:
        return "pending"
    src = (source or "").strip().lower()
    if src in ENRICHABLE_JD_SOURCES:
        return "pending"
    return "short_jd"


def _safe_display_str(val: Optional[str]) -> str:
    """Return a string safe for display; avoid showing 'nan' or 'None' from bad data."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    return s or ""


def annotate_saved_flags(user, track: Optional[str], jobs: List[JobPayload]) -> List[JobPayload]:
    """Set ``is_saved`` / ``is_liked`` on each payload from track-scoped feedback actions."""
    if not jobs:
        return jobs
    saved_ids = saved_listing_id_set(user, track)
    liked_ids = liked_listing_id_set(user, track)
    for payload in jobs:
        payload.is_saved = payload.id in saved_ids
        payload.is_liked = payload.id in liked_ids
    return jobs


def _job_to_payload(job: JobListing, *, snippet: Optional[str] = None) -> JobPayload:
    if snippet is None:
        snippet = (job.description or "")[:300].replace("\n", " ")
    snippet = _safe_display_str(snippet) or (job.description or "")[:300].replace("\n", " ")
    src = job.source or ""
    return JobPayload(
        id=job.id,
        title=_safe_display_str(job.title) or "Untitled",
        company_name=_safe_display_str(job.company_name) or "—",
        location=_safe_display_str(job.location),
        snippet=_safe_display_str(snippet),
        description=job.description or "",
        url=job.url or "",
        source=src,
        source_display=format_job_source_label(src),
        posted_at=job.posted_at,
        fetched_at=job.fetched_at,
    )


def rehydrate_job_payloads(jobs: list) -> List[JobPayload]:
    """
    Restore JobPayload objects from session-cached dicts.

    ``model_dump(mode="json")`` serializes datetimes to ISO strings; Django's
    ``timesince`` filter requires real datetime instances.
    """
    if not jobs:
        return []
    out: List[JobPayload] = []
    for job in jobs:
        if isinstance(job, JobPayload):
            out.append(job)
            continue
        if not isinstance(job, dict):
            continue
        data = dict(job)
        data["posted_at"] = _parse_date_posted(data.get("posted_at"))
        data["fetched_at"] = _parse_date_posted(data.get("fetched_at"))
        try:
            out.append(JobPayload(**data))
        except Exception:
            logger.warning("Skipping invalid cached job payload id=%s", data.get("id"), exc_info=True)
    return out


def _tokenize_for_bm25(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"\w+", text.lower())


INTERACTIVE_JOB_SEARCH_LOCK_TIMEOUT = 120  # 2 minutes max safety timeout


def get_interactive_job_search_lock_key(user_id: int | str) -> str:
    """Per-tenant lock key preventing overlapping live scrapes for the same user."""
    return f"job_search_interactive:u{user_id}"


def run_job_search_core(
    *,
    user,
    search_term: str,
    location: Optional[str] = None,
    track: Optional[str] = None,
    results_wanted: int = 50,
    site_name: Optional[List[str]] = None,
    sort: str = "focus",
    return_audit_log: bool = False,
) -> Tuple[int, int, List[JobPayload], List[dict]] | Tuple[int, int, List[JobPayload], List[dict], List[dict]]:
    """
    Fetch jobs, upsert JobListing, apply filters and ranking. No request/session.

    Returns:
        (jobs_fetched, jobs_after_filter, list[JobPayload], refs_for_cache)
        or (jobs_fetched, jobs_after_filter, list[JobPayload], refs_for_cache, audit_items) if return_audit_log=True
    """
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required")

    from django.core.cache import cache
    from .subscriptions import METRIC_JOB_SEARCHES, QuotaExceeded, consume_quota

    user_id = getattr(user, "id", None) if user is not None and getattr(user, "is_authenticated", False) else None
    lock_key = get_interactive_job_search_lock_key(user_id) if user_id else None
    if lock_key and not cache.add(lock_key, 1, INTERACTIVE_JOB_SEARCH_LOCK_TIMEOUT):
        raise RuntimeError("A job search is already in progress for your account. Please wait for it to complete.")

    try:
        try:
            consume_quota(user, METRIC_JOB_SEARCHES, 1)
        except QuotaExceeded as exc:
            raise ValueError(str(exc)) from exc

        results_wanted = results_wanted or getattr(settings, "JOB_SEARCH_DEFAULT_RESULTS", 50)
        site_name = normalize_site_names(site_name)
        norm_track = normalize_track_slug(track, user)
        repost_cooldown_days = getattr(settings, "PIPELINE_DELETED_REPOST_COOLDOWN_DAYS", 14)
        disliked_listing_ids = disliked_listing_id_set(user, track)
        hidden_listing_ids = hidden_listing_id_set(user, track, cooldown_days=repost_cooldown_days)
        from .models import UserDisqualifier

        disqualifier_pattern = build_disqualifier_pattern(
            list(UserDisqualifier.objects.for_user(user).values_list("phrase", flat=True))
        )
        jobs_with_meta: List[Tuple[JobListing, JobPayload]] = []

        hours_old = getattr(settings, "JOB_SEARCH_HOURS_OLD", 168)
        fetch_kwargs = {
            "search_term": search_term.strip(),
            "location": (location or "").strip() or None,
            "site_name": site_name,
            "results_wanted": results_wanted,
            "hours_old": hours_old,
        }
        try:
            raw = fetch_jobs(**fetch_kwargs)
        except Exception as e:
            raise RuntimeError(f"Job fetch failed: {e}") from e

        jobs_fetched = len(raw or [])
        logger.info(
            "[job_search_core] JobSpy returned %d jobs (requested %d, hours_old=%s)",
            jobs_fetched,
            results_wanted,
            hours_old,
        )
        if not raw and results_wanted > 50:
            raw = fetch_jobs(**{**fetch_kwargs, "results_wanted": 50})
            jobs_fetched = len(raw or [])
            logger.info("[job_search_core] JobSpy retry(50) returned %d jobs", jobs_fetched)

        refs_for_cache: List[dict] = []
        audit_items: List[dict] = []
        audit_by_job_id: dict[int, dict] = {}

        for r in raw or []:
            job, _ = upsert_job_listing_from_fetch(r)
            refs_for_cache.append({"source": job.source, "external_id": job.external_id})

            audit_entry = {
                "job_id": job.id,
                "title": job.title or "",
                "company": job.company_name or "",
                "location": job.location or "",
                "source": job.source or "",
                "url": job.url or "",
                "date_posted": job.date_posted.isoformat() if getattr(job, "date_posted", None) else "",
                "disposition": "pending",
                "reason": "",
            }
            audit_items.append(audit_entry)
            audit_by_job_id[job.id] = audit_entry

            if job.id in disliked_listing_ids:
                audit_entry["disposition"] = "eliminated_disliked"
                audit_entry["reason"] = "Eliminated: In your disliked jobs (negative preference)"
                continue

            if job.id in hidden_listing_ids:
                audit_entry["disposition"] = "eliminated_hidden"
                audit_entry["reason"] = f"Eliminated: Hidden from this profile (within {repost_cooldown_days}-day cooldown)"
                continue

            if disqualifier_pattern:
                desc = (job.description or "")
                dq_match = disqualifier_pattern.search(desc)
                if dq_match:
                    matched_kw = dq_match.group(0)
                    audit_entry["disposition"] = "eliminated_disqualifier"
                    audit_entry["reason"] = f"Eliminated: Matched disqualifier keyword '{matched_kw}'"
                    continue

            snippet = (job.description or "")[:300].replace("\n", " ")
            pl = _job_to_payload(job, snippet=snippet)
            jobs_with_meta.append((job, pl))

        jobs_after_filter = len(jobs_with_meta)
        logger.info("[job_search_core] After filter: %d jobs", jobs_after_filter)

        elimination_details: dict[int, dict] = {}
        jobs_out = rank_and_filter_jobs(
            jobs_with_meta,
            norm_track,
            user=user,
            sort=sort,
            elimination_details=elimination_details,
        )
        annotate_saved_flags(user, norm_track, jobs_out)

        # Mark jobs that passed initial filter but were dropped during ranking / dedupe
        kept_job_ids = {p.id for p in jobs_out}
        for job, _pl in jobs_with_meta:
            if job.id not in kept_job_ids:
                entry = audit_by_job_id.get(job.id)
                if entry and entry["disposition"] == "pending":
                    det = elimination_details.get(job.id)
                    if det:
                        entry["disposition"] = det.get("disposition", "eliminated_low_preference")
                        entry["reason"] = det.get("reason", "Eliminated: Below preference matching threshold")
                    else:
                        entry["disposition"] = "eliminated_low_preference"
                        entry["reason"] = "Eliminated: Below preference matching threshold"

        if return_audit_log:
            return (jobs_fetched, jobs_after_filter, jobs_out, refs_for_cache, audit_items)
        return (jobs_fetched, jobs_after_filter, jobs_out, refs_for_cache)

    finally:
        if lock_key:
            cache.delete(lock_key)


def _rank_jobs_with_meta(
    jobs_with_meta: List[Tuple[JobListing, JobPayload]],
    track: Optional[str],
    *,
    user,
) -> List[JobPayload]:
    """Apply preference ranking and disliked penalty to (job, payload) list. Returns sorted list of JobPayload."""
    prefs = get_preference_vectors(user=user, track=track)
    liked_jobs = (prefs[2] if prefs and len(prefs) > 2 else []) or []
    if prefs and not liked_jobs:
        liked_jobs = get_liked_jobs_for_focus_reason(user=user, track=track)

    jobs_out: List[JobPayload] = []
    if prefs and jobs_with_meta:
        try:
            jobs = [j for j, _ in jobs_with_meta]
            result = rank_jobs_by_preference(jobs, user=user, track=track)
            if result is None:
                raise RuntimeError("rank_jobs_by_preference returned None")
            scores, title_vecs, full_vecs_per_job = result
            alpha = getattr(settings, "JOB_FOCUS_TITLE_WEIGHT", 0.55)
            top_k = getattr(settings, "JOB_FOCUS_ROLE_TOP_K", 5)
            kw_weight = getattr(settings, "JOB_FOCUS_KEYWORD_WEIGHT", 0.0)
            role_batch = [(j.title or "", j.description or "") for j in jobs]
            bm25_norm_scores: List[float] = [0.0] * len(jobs_with_meta)
            if kw_weight > 0 and liked_jobs:
                from .models import JobListing as _JL
                role_texts = [
                    embedding_module.extract_role_description(desc or "", title or "", max_chars=1000)
                    for title, desc in role_batch
                ]
                docs_tokens = [_tokenize_for_bm25(t) for t in role_texts]
                liked_ids = [lid for (lid, _, _, _, _) in liked_jobs]
                liked_map = {j.id: j for j in _JL.objects.filter(id__in=liked_ids)} if liked_ids else {}
                query_tokens: List[str] = []
                for (lid, _ltitle, _lcompany, _ltvec, _lrole_vecs) in liked_jobs:
                    lj = liked_map.get(lid)
                    if not lj:
                        continue
                    q_text = embedding_module.extract_role_description(
                        lj.description or "", lj.title or "", max_chars=1000
                    )
                    query_tokens.extend(_tokenize_for_bm25(q_text))
                if query_tokens and any(docs_tokens):
                    try:
                        from rank_bm25 import BM25Okapi
                        bm25 = BM25Okapi(docs_tokens)
                        raw_scores = bm25.get_scores(query_tokens)
                        if raw_scores is not None and len(raw_scores):
                            max_s = max(raw_scores)
                            min_s = min(raw_scores)
                            if max_s > min_s:
                                bm25_norm_scores = [
                                    float((s - min_s) / (max_s - min_s)) for s in raw_scores
                                ]
                            else:
                                bm25_norm_scores = [0.5] * len(raw_scores)
                    except Exception as e:
                        logger.warning("BM25 scoring failed; ignoring keyword boost: %s", e)

            scored = []
            for idx, ((job, job_payload), base_score, tvec, full_vecs) in enumerate(
                zip(jobs_with_meta, scores, title_vecs, full_vecs_per_job)
            ):
                score = base_score
                if tvec is not None:
                    if kw_weight > 0 and bm25_norm_scores:
                        try:
                            kw_norm = bm25_norm_scores[idx] if idx < len(bm25_norm_scores) else 0.0
                            kw_sim = 2.0 * kw_norm - 1.0
                            score = (1.0 - kw_weight) * base_score + kw_weight * kw_sim
                        except Exception:
                            pass
                    normalized = max(-1.0, min(1.0, score))
                    percent = int(round(((normalized + 1.0) / 2.0) * 100))
                    job_payload.focus_score = round(score, 4)
                    job_payload.focus_percent = percent
                    margin_norm = None
                    try:
                        from .embeddings import embed_full, cosine_similarity
                        full_vec = embed_full(job.title or "", job.description or "")
                        if full_vec is not None:
                            like_centroid = prefs[0]
                            like_sim = cosine_similarity(full_vec, like_centroid)
                            like_percent = int(round(((max(-1.0, min(1.0, like_sim)) + 1.0) / 2.0) * 100))
                            if len(prefs) > 1 and prefs[1] is not None:
                                dislike_centroid = prefs[1]
                                dislike_sim = cosine_similarity(full_vec, dislike_centroid)
                                dislike_percent = int(round(((max(-1.0, min(1.0, dislike_sim)) + 1.0) / 2.0) * 100))
                            else:
                                dislike_percent = 0
                            margin = like_percent - dislike_percent
                            job_payload.preference_margin_percent = margin
                            margin_norm = margin / 100.0
                    except Exception:
                        pass
                    sort_metric = margin_norm if margin_norm is not None else score
                    scored.append((sort_metric, job_payload, tvec, full_vecs))
                    if liked_jobs:
                        sims = [
                            (
                                _gated_combined_score(
                                    embedding_module.cosine_similarity(tvec, lt),
                                    embedding_module.role_similarity_topk_mean(full_vecs, lrole, top_k),
                                    alpha,
                                ),
                                title, company,
                            )
                            for _, title, company, lt, lrole in liked_jobs
                        ]
                        sims.sort(key=lambda x: -x[0])
                        top = sims[:3]
                        job_payload.focus_reason = [
                            {
                                "title": t,
                                "company_name": c,
                                "similarity_percent": int(round(((max(-1, min(1, s)) + 1) / 2) * 100)),
                            }
                            for s, t, c in top
                        ]
                else:
                    scored.append((score, job_payload, None, None))
                    job_payload.focus_score = None
                    job_payload.focus_percent = None
                    job_payload.focus_reason = None
            scored.sort(key=lambda x: -x[0])
            jobs_out = [p for _, p, _tvec, _role_vecs in scored]
        except Exception as e:
            logger.warning("Preference ranking failed, returning unsorted: %s", e)
            jobs_out = [p for _, p in jobs_with_meta]
    else:
        jobs_out = [p for _, p in jobs_with_meta]

    return jobs_out


def _apply_auto_dislike(
    ranked: List[JobPayload],
    track: Optional[str],
    user,
    dropped_details: Optional[dict] = None,
) -> List[JobPayload]:
    """Filter out jobs with low preference margin (margin < -5); records drop details without mutating user action history."""
    out: List[JobPayload] = []
    for payload_obj in ranked:
        margin = getattr(payload_obj, "preference_margin_percent", None)
        if margin is not None and margin < -5:
            if dropped_details is not None:
                dropped_details[payload_obj.id] = {
                    "disposition": "eliminated_low_preference",
                    "reason": f"Eliminated: Low preference margin ({margin}%, below -5% guardrail)",
                }
            continue
        out.append(payload_obj)
    return out



def _apply_disliked_penalty_and_final_sort(
    jobs_out: List[JobPayload],
    track: Optional[str],
    *,
    user,
    dropped_details: Optional[dict] = None,
) -> List[JobPayload]:
    """Apply disliked-similarity penalty and final sort by preference_margin_percent."""
    try:
        disliked_embeddings = get_disliked_embeddings(user=user, track=track)
    except Exception as e:
        logger.warning("Loading disliked embeddings failed, skipping penalty: %s", e)
        disliked_embeddings = []
    has_margin_based_sort = any(
        getattr(p, "preference_margin_percent", None) is not None for p in jobs_out
    )
    if disliked_embeddings and jobs_out and not has_margin_based_sort:
        try:
            penalty_weight = getattr(settings, "JOB_DISLIKED_SIMILARITY_PENALTY_WEIGHT", 0.4)
            threshold = getattr(settings, "JOB_DISLIKED_SIMILARITY_THRESHOLD", 0.3)
            hide_threshold = getattr(settings, "JOB_DISLIKED_SIMILARITY_HIDE_THRESHOLD", None)
            job_ids = [p.id for p in jobs_out]
            job_map = {j.id: j for j in JobListing.objects.filter(id__in=job_ids)}
            ordered_jobs = [job_map[jid] for jid in job_ids if jid in job_map]
            if len(ordered_jobs) == len(job_ids):
                full_batch = [(j.title or "", j.description or "") for j in ordered_jobs]
                result_vecs = embedding_module.embed_full_batch(full_batch)
                disliked_vecs = [emb for _, emb in disliked_embeddings]
                scored_with_penalty = []
                for i, p in enumerate(jobs_out):
                    p.similar_to_disliked_percent = None
                    vec = result_vecs[i] if i < len(result_vecs) else None
                    if vec is None:
                        disliked_sim = 0.0
                    else:
                        sims = [
                            embedding_module.cosine_similarity(vec, d_emb)
                            for d_emb in disliked_vecs
                        ]
                        disliked_sim = max(sims) if sims else 0.0
                    disliked_sim = max(0.0, min(1.0, (disliked_sim + 1.0) / 2.0))
                    p.similar_to_disliked_percent = int(round(disliked_sim * 100))
                    if hide_threshold is not None and p.similar_to_disliked_percent >= hide_threshold:
                        if dropped_details is not None:
                            dropped_details[p.id] = {
                                "disposition": "eliminated_disliked_similarity",
                                "reason": f"Eliminated: High similarity to disliked jobs ({p.similar_to_disliked_percent}%)",
                            }
                        continue
                    if disliked_sim < threshold:
                        penalty = 0.0
                    else:
                        penalty = penalty_weight * disliked_sim

                    base = p.focus_score if p.focus_score is not None else -1.0
                    sort_key = base - penalty
                    if penalty > 0 and p.focus_percent is not None:
                        focus_raw = (p.focus_score + 1.0) / 2.0 if p.focus_score is not None else 0.5
                        adjusted = max(0.0, min(1.0, focus_raw - penalty))
                        p.focus_percent_after_penalty = int(round(adjusted * 100))
                    scored_with_penalty.append((sort_key, p))
                scored_with_penalty.sort(key=lambda x: -x[0])
                jobs_out = [p for _, p in scored_with_penalty]
        except Exception as e:
            logger.warning("Disliked similarity penalty failed: %s", e)

    if any(getattr(p, "preference_margin_percent", None) is not None for p in jobs_out):
        jobs_out = list(jobs_out)
        jobs_out.sort(
            key=lambda p: (
                0 if getattr(p, "preference_margin_percent", None) is not None else 1,
                getattr(p, "preference_margin_percent", -999),
            ),
            reverse=True,
        )
    return jobs_out


def sort_job_payloads(jobs: List[JobPayload], sort_by: str = "match") -> List[JobPayload]:
    """
    Sort JobPayload objects by requested criteria:
    - 'match' / 'focus': Match % / fit score descending, then freshness (date posted) descending
    - 'latest' / 'freshness' / 'date' / 'newest': Date posted / fetched descending, then match % descending
    - 'oldest': Date posted / fetched ascending, then match % descending
    """
    from datetime import datetime
    from django.utils import timezone

    def _get_job_date(p: JobPayload):
        dt = getattr(p, "posted_at", None) or getattr(p, "fetched_at", None)
        if dt is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.utc)
        return dt

    def _get_job_match_score(p: JobPayload) -> float:
        if getattr(p, "matching_score", None) is not None:
            return float(p.matching_score)
        if getattr(p, "focus_percent_after_penalty", None) is not None:
            return float(p.focus_percent_after_penalty)
        if getattr(p, "focus_percent", None) is not None:
            return float(p.focus_percent)
        if getattr(p, "preference_margin_percent", None) is not None:
            return float(p.preference_margin_percent)
        return -999.0

    normalized_sort = (sort_by or "match").strip().lower()
    if normalized_sort in ("latest", "freshness", "date", "newest"):
        return sorted(
            jobs,
            key=lambda p: (
                _get_job_date(p),
                _get_job_match_score(p) > -900.0,
                _get_job_match_score(p),
                getattr(p, "id", 0),
            ),
            reverse=True,
        )
    elif normalized_sort == "oldest":
        return sorted(
            jobs,
            key=lambda p: (
                _get_job_date(p),
                -(_get_job_match_score(p) if _get_job_match_score(p) > -900.0 else -999.0),
                -getattr(p, "id", 0),
            ),
        )
    else:  # "match", "focus", default
        return sorted(
            jobs,
            key=lambda p: (
                _get_job_match_score(p) > -900.0,
                _get_job_match_score(p),
                _get_job_date(p),
                getattr(p, "id", 0),
            ),
            reverse=True,
        )


def rank_and_filter_jobs(
    jobs_with_meta: List[Tuple[JobListing, JobPayload]],
    track: Optional[str],
    *,
    user,
    sort: str = "match",
    elimination_details: Optional[dict] = None,
) -> List[JobPayload]:
    """
    Single entry point for preference ranking: score, auto-dislike margin < -5,
    apply disliked penalty, and final sort according to requested sort option.
    """
    ranked = _rank_jobs_with_meta(jobs_with_meta, track, user=user)
    jobs_out = _apply_auto_dislike(ranked, track, user, dropped_details=elimination_details)
    jobs_out = _apply_disliked_penalty_and_final_sort(
        jobs_out, track, user=user, dropped_details=elimination_details
    )

    # NEW: Apply Ollama Guard to top 10 results to reduce noise (e.g. seniority mismatch)
    from django.conf import settings

    if getattr(settings, "OLLAMA_GUARD_ENABLED", False):
        try:
            from .services import run_ollama_guard_on_payloads

            before_guard_ids = {p.id for p in jobs_out}
            jobs_out = run_ollama_guard_on_payloads(jobs_out[:10], track, user=user) + jobs_out[10:]
            if elimination_details is not None:
                after_guard_ids = {p.id for p in jobs_out}
                for dropped_id in (before_guard_ids - after_guard_ids):
                    elimination_details[dropped_id] = {
                        "disposition": "eliminated_guard",
                        "reason": "Eliminated: Filtered by Ollama Guard (seniority or role mismatch)",
                    }
        except Exception as e:
            logger.warning("Ollama Guard failed, skipping: %s", e)

    from .job_dedupe import dedupe_payloads_by_title_company

    before = len(jobs_out)
    jobs_out = dedupe_payloads_by_title_company(jobs_out, dropped_details=elimination_details)
    if len(jobs_out) < before:
        logger.info(
            "[job_search] title/company dedupe: %d → %d jobs",
            before,
            len(jobs_out),
        )

    return sort_job_payloads(jobs_out, sort_by=sort)


def recompute_preferences_for_jobs(
    job_listings: List[JobListing],
    track: Optional[str],
    *,
    user,
) -> Dict[int, dict]:
    """
    Compute focus %, post-penalty %, and preference margin for a batch of jobs.

    Returns mapping: job_id -> {
        "focus_percent": int | None,
        "focus_after_penalty": int | None,
        "preference_margin": int | None,
    }
    """
    if not job_listings:
        return {}
    jobs_with_meta: List[Tuple[JobListing, JobPayload]] = []
    for job in job_listings:
        snippet = (job.description or "")[:300].replace("\n", " ")
        jobs_with_meta.append((job, _job_to_payload(job, snippet=snippet)))
    ranked_payloads = _rank_jobs_with_meta(jobs_with_meta, track, user=user)
    by_id: Dict[int, dict] = {}
    for payload in ranked_payloads:
        jid = payload.id
        by_id[jid] = {
            "focus_percent": getattr(payload, "focus_percent", None),
            "focus_after_penalty": getattr(payload, "focus_percent_after_penalty", None),
            "preference_margin": getattr(payload, "preference_margin_percent", None),
        }
    return by_id


def persist_preference_metrics_for_jobs(
    *,
    user,
    track: str,
    job_listings: List[JobListing],
    payloads: Optional[List[JobPayload]] = None,
    scores: Optional[Dict[int, dict]] = None,
) -> int:
    """
    Write JobListingTrackMetrics for (owner, track, job_listing).

    Prefer ``payloads`` or ``scores`` from a recent rank pass to avoid a second
    embedding batch; recompute only for rows still missing focus or margin.

    Returns the number of rows written.
    """
    if not job_listings:
        return 0
    track_slug = (track or "").strip().lower()
    if not track_slug:
        return 0

    from .search_profile_scope import dual_write_track_fields

    dw = dual_write_track_fields(user=user, slug=track_slug)

    if scores is None:
        scores = {}
    if payloads:
        for payload in payloads:
            scores[payload.id] = {
                "focus_percent": getattr(payload, "focus_percent", None),
                "focus_after_penalty": getattr(payload, "focus_percent_after_penalty", None),
                "preference_margin": getattr(payload, "preference_margin_percent", None),
            }

    needs_recompute = [
        job
        for job in job_listings
        if not scores.get(job.id)
        or (
            scores[job.id].get("focus_percent") is None
            and scores[job.id].get("preference_margin") is None
        )
    ]
    if needs_recompute:
        recomputed = recompute_preferences_for_jobs(needs_recompute, track=track_slug, user=user)
        scores.update(recomputed)

    now = timezone.now()
    written = 0
    for job in job_listings:
        data = scores.get(job.id) or {}
        JobListingTrackMetrics.objects.update_or_create(
            owner=user,
            job_listing=job,
            track=track_slug,
            defaults={
                "search_profile": dw["search_profile"],
                "focus_percent": data.get("focus_percent"),
                "focus_after_penalty": data.get("focus_after_penalty"),
                "preference_margin": data.get("preference_margin"),
                "last_scored_at": now,
            },
        )
        written += 1
    return written


def pipeline_jobs_to_payloads(
    job_listings: List[JobListing],
    track: Optional[str],
    *,
    user,
) -> List[JobPayload]:
    """
    Build JobPayload list for pipeline view.

    Uses cached preference metrics from JobListingTrackMetrics only. Does not
    recompute preferences on demand; if metrics are missing, jobs are shown
    without Fit / Pref badges until the background task fills them.

    ``user`` is required so that metric/pipeline lookups are scoped to the
    owning tenant and never bleed between users sharing the same JobListing rows.
    """
    if not job_listings:
        return []
    track_slug = (track or "").strip().lower() or None
    job_ids = [j.id for j in job_listings]

    metrics_map: Dict[int, JobListingTrackMetrics] = {}
    if track_slug and job_ids:
        for m in JobListingTrackMetrics.objects.filter(
            owner=user, track=track_slug, job_listing_id__in=job_ids
        ):
            metrics_map[m.job_listing_id] = m

    # Interview probability + reasoning (stored on PipelineEntry; persists across stages).
    entry_map: Dict[int, PipelineEntry] = {}
    if job_ids:
        entries_qs = PipelineEntry.objects.filter(
            owner=user,
            job_listing_id__in=job_ids,
            removed_at__isnull=True,
        )
        for e in entries_qs:
            if e.job_listing_id not in entry_map or (track_slug and e.track == track_slug):
                entry_map[e.job_listing_id] = e

    jobs_with_meta: List[Tuple[JobListing, JobPayload]] = []
    for job in job_listings:
        snippet = (job.description or "")[:300].replace("\n", " ")
        payload = _job_to_payload(job, snippet=snippet)
        metrics = metrics_map.get(job.id)
        if metrics:
            payload.focus_percent = metrics.focus_percent
            payload.focus_percent_after_penalty = metrics.focus_after_penalty
            payload.preference_margin_percent = metrics.preference_margin

        interview_entry = entry_map.get(job.id)
        if interview_entry:
            payload.vetting_match_score = getattr(interview_entry, "vetting_match_score", None)
            payload.interview_probability = interview_entry.vetting_interview_probability
            payload.interview_reasoning = interview_entry.vetting_interview_reasoning
            payload.zero_llm_core_matches = interview_entry.vetting_core_competencies or []
            payload.zero_llm_stretch_skills = interview_entry.vetting_stretch_skills or []
        payload.interview_status = resolve_interview_display_status(
            description=job.description or "",
            interview_probability=payload.interview_probability,
            source=job.source or "",
        )
        jobs_with_meta.append((job, payload))

    # Sort descending by preference margin, then focus percent; jobs without
    # metrics are placed last.
    def _sort_key(item: Tuple[JobListing, JobPayload]):
        _job, pl = item
        margin = getattr(pl, "preference_margin_percent", None)
        focus = getattr(pl, "focus_percent", None)
        return (
            0 if margin is not None else 1,
            margin if margin is not None else -999,
            focus if focus is not None else -999,
        )

    jobs_with_meta.sort(key=_sort_key, reverse=True)
    payloads = [p for _j, p in jobs_with_meta]
    return annotate_saved_flags(user, track_slug, payloads)
