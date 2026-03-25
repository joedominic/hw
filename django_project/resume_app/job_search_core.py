"""
Request-agnostic job search: fetch, filter, rank.
Used by jobs_search API and by run_job_search_task (pipeline).
"""
import logging
import re
from typing import List, Optional, Tuple, Dict

from django.conf import settings

from .models import JobListing, JobListingAction, JobListingTrackMetrics, PipelineEntry
from .job_sources import DEFAULT_SITE_NAMES, fetch_jobs
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
    get_disqualifier_phrases,
    build_disqualifier_pattern,
    job_matches_disqualifiers,
)
from . import embeddings as embedding_module
from .utils import format_job_source_label

logger = logging.getLogger(__name__)


def _safe_display_str(val: Optional[str]) -> str:
    """Return a string safe for display; avoid showing 'nan' or 'None' from bad data."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    return s or ""


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
        url=job.url or "",
        source=src,
        source_display=format_job_source_label(src),
        fetched_at=job.fetched_at,
    )


def _tokenize_for_bm25(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"\w+", text.lower())


def run_job_search_core(
    search_term: str,
    location: Optional[str] = None,
    track: Optional[str] = None,
    results_wanted: int = 50,
    site_name: Optional[List[str]] = None,
    sort: str = "focus",
) -> Tuple[int, int, List[JobPayload], List[dict]]:
    """
    Fetch jobs, upsert JobListing, apply filters and ranking. No request/session.

    Returns:
        (jobs_fetched, jobs_after_filter, list[JobPayload], refs_for_cache)
    """
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required")

    results_wanted = results_wanted or getattr(settings, "JOB_SEARCH_DEFAULT_RESULTS", 50)
    site_name = site_name or list(DEFAULT_SITE_NAMES)
    disliked_listing_ids = set(
        JobListingAction.objects.filter(action=JobListingAction.ActionType.DISLIKED).values_list(
            "job_listing_id", flat=True
        )
    )
    disqualifier_pattern = build_disqualifier_pattern(get_disqualifier_phrases())
    jobs_with_meta: List[Tuple[JobListing, JobPayload]] = []

    try:
        raw = fetch_jobs(
            search_term=search_term.strip(),
            location=(location or "").strip() or None,
            site_name=site_name,
            results_wanted=results_wanted,
        )
    except Exception as e:
        raise RuntimeError(f"Job fetch failed: {e}") from e

    jobs_fetched = len(raw or [])
    logger.info("[job_search_core] JobSpy returned %d jobs (requested %d)", jobs_fetched, results_wanted)
    if not raw and results_wanted > 50:
        raw = fetch_jobs(
            search_term=search_term.strip(),
            location=(location or "").strip() or None,
            site_name=site_name,
            results_wanted=50,
        )
        jobs_fetched = len(raw or [])
        logger.info("[job_search_core] JobSpy retry(50) returned %d jobs", jobs_fetched)

    refs_for_cache: List[dict] = []
    for r in raw or []:
        desc = r.get("description", "") or ""
        defaults = {
            "title": r["title"],
            "company_name": r["company_name"],
            "location": r.get("location", ""),
            "description": desc,
            "url": r.get("job_url", ""),
        }
        job, _ = JobListing.objects.update_or_create(
            source=r["source"],
            external_id=r["external_id"],
            defaults=defaults,
        )
        refs_for_cache.append({"source": job.source, "external_id": job.external_id})
        if job.id in disliked_listing_ids:
            continue
        if job_matches_disqualifiers(job, disqualifier_pattern):
            continue
        snippet = (job.description or "")[:300].replace("\n", " ")
        pl = _job_to_payload(job, snippet=snippet)
        jobs_with_meta.append((job, pl))

    jobs_after_filter = len(jobs_with_meta)
    logger.info("[job_search_core] After filter: %d jobs", jobs_after_filter)

    jobs_out = rank_and_filter_jobs(jobs_with_meta, track)
    return (jobs_fetched, jobs_after_filter, jobs_out, refs_for_cache)


def _rank_jobs_with_meta(
    jobs_with_meta: List[Tuple[JobListing, JobPayload]],
    track: Optional[str],
) -> List[JobPayload]:
    """Apply preference ranking and disliked penalty to (job, payload) list. Returns sorted list of JobPayload."""
    prefs = get_preference_vectors(track=track)
    liked_jobs = (prefs[2] if prefs and len(prefs) > 2 else []) or []
    if prefs and not liked_jobs:
        liked_jobs = get_liked_jobs_for_focus_reason(track=track)

    jobs_out: List[JobPayload] = []
    if prefs and jobs_with_meta:
        try:
            jobs = [j for j, _ in jobs_with_meta]
            result = rank_jobs_by_preference(jobs, track=track)
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


def _apply_auto_dislike(ranked: List[JobPayload]) -> List[JobPayload]:
    """Apply auto-dislike for margin < -5; mutate DB and return filtered list (read-only ranking stays pure)."""
    out: List[JobPayload] = []
    for payload_obj in ranked:
        margin = getattr(payload_obj, "preference_margin_percent", None)
        if margin is not None and margin < -5:
            try:
                job_obj = JobListing.objects.filter(id=payload_obj.id).first()
                if job_obj:
                    JobListingAction.objects.get_or_create(
                        job_listing=job_obj,
                        action=JobListingAction.ActionType.DISLIKED,
                    )
                    invalidate_preference_cache()
                    invalidate_disliked_embeddings_cache()
            except Exception as e:
                logger.warning("Auto-exclude (margin< -5) failed for job %s: %s", payload_obj.id, e)
            continue
        out.append(payload_obj)
    return out


def _apply_disliked_penalty_and_final_sort(
    jobs_out: List[JobPayload],
    track: Optional[str],
) -> List[JobPayload]:
    """Apply disliked-similarity penalty and final sort by preference_margin_percent."""
    try:
        disliked_embeddings = get_disliked_embeddings(track=track)
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


def rank_and_filter_jobs(
    jobs_with_meta: List[Tuple[JobListing, JobPayload]],
    track: Optional[str],
) -> List[JobPayload]:
    """
    Single entry point for preference ranking: score, auto-dislike margin < -5,
    apply disliked penalty, and final sort. Used by run_job_search_core and by
    jobs_search cache path. Preserves preference_margin_percent as primary sort.
    """
    ranked = _rank_jobs_with_meta(jobs_with_meta, track)
    jobs_out = _apply_auto_dislike(ranked)
    return _apply_disliked_penalty_and_final_sort(jobs_out, track)


def recompute_preferences_for_jobs(
    job_listings: List[JobListing],
    track: Optional[str],
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
    ranked_payloads = _rank_jobs_with_meta(jobs_with_meta, track)
    by_id: Dict[int, dict] = {}
    for payload in ranked_payloads:
        jid = payload.id
        by_id[jid] = {
            "focus_percent": getattr(payload, "focus_percent", None),
            "focus_after_penalty": getattr(payload, "focus_percent_after_penalty", None),
            "preference_margin": getattr(payload, "preference_margin_percent", None),
        }
    return by_id


def pipeline_jobs_to_payloads(
    job_listings: List[JobListing],
    track: Optional[str],
) -> List[JobPayload]:
    """
    Build JobPayload list for pipeline view.

    Uses cached preference metrics from JobListingTrackMetrics only. Does not
    recompute preferences on demand; if metrics are missing, jobs are shown
    without Fit / Pref badges until the background task fills them.
    """
    if not job_listings:
        return []
    track_slug = (track or "").strip().lower() or None
    job_ids = [j.id for j in job_listings]

    metrics_map: Dict[int, JobListingTrackMetrics] = {}
    if track_slug and job_ids:
        for m in JobListingTrackMetrics.objects.filter(
            track=track_slug, job_listing_id__in=job_ids
        ):
            metrics_map[m.job_listing_id] = m

    # Vetting-only: interview probability + reasoning (stored on PipelineEntry).
    interview_map: Dict[int, PipelineEntry] = {}
    if track_slug and job_ids:
        for e in PipelineEntry.objects.filter(
            track=track_slug,
            job_listing_id__in=job_ids,
            stage=PipelineEntry.Stage.VETTING,
            removed_at__isnull=True,
        ):
            interview_map[e.job_listing_id] = e

    jobs_with_meta: List[Tuple[JobListing, JobPayload]] = []
    for job in job_listings:
        snippet = (job.description or "")[:300].replace("\n", " ")
        payload = _job_to_payload(job, snippet=snippet)
        metrics = metrics_map.get(job.id)
        if metrics:
            payload.focus_percent = metrics.focus_percent
            payload.focus_percent_after_penalty = metrics.focus_after_penalty
            payload.preference_margin_percent = metrics.preference_margin

        interview_entry = interview_map.get(job.id)
        if interview_entry:
            payload.interview_probability = interview_entry.vetting_interview_probability
            payload.interview_reasoning = interview_entry.vetting_interview_reasoning
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
    return [p for _j, p in jobs_with_meta]
