"""
Job search, fit-check match, embeddings, and pipeline/saved-job actions (JSON).

Mounted under `/api/resume/jobs/…` via `api.router`. Consumed by job search UI and pipeline partials.
"""
import logging
import re
from typing import List, Optional

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from .models import (
    JobListing,
    JobMatchResult,
    UserResume,
    LLMProviderConfig,
    JobListingAction,
    JobListingEmbedding,
    UserDisqualifier,
    PipelineEntry,
    Track,
)
from .job_sources import DEFAULT_SITE_NAMES, fetch_jobs
from .job_search_core import rank_and_filter_jobs, run_job_search_core, pipeline_jobs_to_payloads
from .services import parse_pdf
from .crypto import decrypt_api_key
from .agents import (
    get_llm,
    run_fit_check,
    run_matching,
    _llm_invoke_with_retry,
    build_llm_messages_for_prompt,
)
from .llm_gateway import (
    LLMRequestsDisabled,
    USAGE_QUERY_JOB_INSIGHTS,
    USAGE_QUERY_JOBS_AI_MATCH,
    USAGE_QUERY_JOBS_MATCH_API,
    USAGE_QUERY_KEYWORD_SEARCH_FIT,
)
from . import embeddings as embedding_module
from .preference import (
    get_preference_vectors,
    get_disliked_embeddings,
    invalidate_preference_cache,
    invalidate_disliked_embeddings_cache,
)
from .prompt_store import profile_for_llm, resolve_prompt_parts
from .schemas import (
    JobSearchRequest,
    JobPayload,
    JobDetailPayload,
    JobSearchResponse,
    MatchRequest,
    MatchResponse,
    MarkAppliedRequest,
    KeywordEntry,
    RunKeywordSearchRequest,
    JobMatchPayload,
    RunKeywordSearchResponse,
    AiMatchRequest,
    AiMatchResultItem,
    AiMatchResponse,
    InsightsRequest,
    InsightsResponse,
    ResumeOption,
    DisqualifierAddRequest,
    DisqualifierPayload,
)
from .job_ranking import get_focus_breakdown, get_focus_sentence_alignment, rank_jobs_by_preference
from .llm_services import LLM_PROVIDERS
from .utils import format_job_source_label
from .disqualifiers import (
    get_disqualifier_phrases,
    build_disqualifier_pattern,
    job_matches_disqualifiers,
)
logger = logging.getLogger(__name__)
router = Router()


def _safe_display_str(val: Optional[str]) -> str:
    """Return a string safe for display; avoid showing 'nan' or 'None' from bad data."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    return s or ""


def _job_to_payload(job: JobListing, *, snippet: Optional[str] = None) -> JobPayload:
    """
    Construct a JobPayload from a JobListing.

    snippet: short text snippet to display (falls back to first 300 chars of description).
    """
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


def _get_llm_from_request(provider: Optional[str] = None, model: Optional[str] = None):
    """Resolve LLM using stored config. Prefer provided provider/model."""
    if not provider:
        config = LLMProviderConfig.objects.filter(encrypted_api_key__isnull=False).exclude(encrypted_api_key="").first()
        if not config:
            raise HttpError(400, "No LLM configured. Set up an API key in LLM Config (Resume Optimizer tab) first.")
        provider = config.provider
        if not model:
            model = config.default_model
        api_key = decrypt_api_key(config.encrypted_api_key)
    else:
        if provider not in LLM_PROVIDERS:
            raise HttpError(400, f"llm_provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}")
        config = LLMProviderConfig.objects.filter(provider=provider).first()
        if not config or not config.encrypted_api_key:
            raise HttpError(401, f"No API key stored for {provider}. Connect in LLM Config first.")
        api_key = decrypt_api_key(config.encrypted_api_key)
        if not model:
            model = config.default_model
    return get_llm(provider, api_key, model)


def _resolve_track(track: Optional[str], session_track: Optional[str] = None) -> str:
    """
    Normalize track slug from explicit param or session, falling back to default.
    """
    return (track or session_track or "").strip().lower() or Track.get_default_slug()


# --- Endpoints ---
# Literal paths must come before /{job_listing_id} or "search"/"matches" get matched as id and cause 405/422.


@router.get("/resumes", response=List[ResumeOption])
def jobs_list_resumes(request):
    """List uploaded resumes for dropdown (e.g. Keyword search tab)."""
    resumes = UserResume.objects.order_by("-uploaded_at")[:100]
    return [
        ResumeOption(
            id=r.id,
            uploaded_at=r.uploaded_at.isoformat() if r.uploaded_at else "",
            label=f"Resume #{r.id}: {r.original_filename or 'resume.pdf'} ({r.uploaded_at.strftime('%Y-%m-%d %H:%M') if r.uploaded_at else 'unknown'})",
            track=r.track or "",
        )
        for r in resumes
    ]


@router.get("/pipeline", response=JobSearchResponse)
def pipeline_list(request, track: str = "ic"):
    """List pipeline jobs for a track (focus % computed at view time). Excludes saved jobs."""
    saved_ids = set(
        JobListingAction.objects.filter(action=JobListingAction.ActionType.SAVED).values_list(
            "job_listing_id", flat=True
        )
    )
    track = (track or "").strip().lower() or Track.get_default_slug()
    entries = (
        PipelineEntry.objects.filter(track=track, removed_at__isnull=True)
        .exclude(job_listing_id__in=saved_ids)
        .select_related("job_listing")
        .order_by("-added_at")
    )
    job_listings = [e.job_listing for e in entries]
    jobs_out = pipeline_jobs_to_payloads(job_listings, track=track)
    return JobSearchResponse(jobs=jobs_out, total=len(jobs_out))


@router.post("/pipeline/delete")
def pipeline_delete(request, job_listing_id: int, track: str = "ic"):
    """Soft-delete a job from the pipeline (set removed_at). It will not be re-added by future task runs."""
    track = (track or "").strip().lower() or Track.get_default_slug()
    entries = PipelineEntry.objects.filter(
        job_listing_id=job_listing_id, track=track, removed_at__isnull=True
    )
    if not entries.exists():
        raise HttpError(404, "Pipeline entry not found or already removed.")
    for entry in entries:
        entry.mark_deleted(save=True)
    return {"success": True}


def _jobs_search_site_key(site_name: Optional[List[str]]) -> tuple:
    sites = site_name if site_name else list(DEFAULT_SITE_NAMES)
    if isinstance(sites, str):
        sites = [sites]
    return tuple(sorted(str(s).strip().lower() for s in sites if str(s).strip()))


def _jobs_search_cache_key(payload: JobSearchRequest) -> tuple:
    """Cache key for external fetch only. resume_id is not included so switching resume does not re-fetch."""
    return (
        (payload.search_term or "").strip(),
        (payload.location or "").strip(),
        payload.results_wanted or 50,
        _jobs_search_site_key(payload.site_name),
    )


@router.post("/search", response=JobSearchResponse)
def jobs_search(request, payload: JobSearchRequest):
    """Fetch jobs via JobSpy (or use session cache when params unchanged), upsert JobListing, return list."""
    try:
        payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    except Exception:
        payload_dict = {"search_term": getattr(payload, "search_term", ""), "payload_repr": str(payload)}
    logger.info("jobs/search request payload: %s", payload_dict)

    if not payload.search_term or not payload.search_term.strip():
        raise HttpError(400, "search_term is required")

    resume_id = payload.resume_id
    disliked_listing_ids = set(
        JobListingAction.objects.filter(action=JobListingAction.ActionType.DISLIKED).values_list(
            "job_listing_id", flat=True
        )
    )
    disqualifier_pattern = build_disqualifier_pattern(get_disqualifier_phrases())
    jobs_with_meta = []  # (job, JobPayload)

    cache_key = _jobs_search_cache_key(payload)
    session = getattr(request, "session", None)
    cached = session.get("job_search_cache") if session else None
    use_cache = (
        cached
        and cached.get("params") == cache_key
        and isinstance(cached.get("refs"), list)
        and len(cached["refs"]) > 0
    )

    if use_cache:
        logger.info("[jobs/search] Using cache: %d refs", len(cached["refs"]))
        for ref in cached["refs"]:
            job = JobListing.objects.filter(
                source=ref.get("source"),
                external_id=ref.get("external_id"),
            ).first()
            if not job:
                continue
            if job.id in disliked_listing_ids:
                continue
            if job_matches_disqualifiers(job, disqualifier_pattern):
                continue
            snippet = (job.description or "")[:300].replace("\n", " ")
            pl = _job_to_payload(job, snippet=snippet)
            jobs_with_meta.append((job, pl))
        logger.info("[jobs/search] Cache path: %d jobs after filter", len(jobs_with_meta))
        # Ranking block below uses jobs_with_meta
    else:
        try:
            _jobs_fetched, _jobs_after_filter, jobs_out_from_core, refs_for_cache = run_job_search_core(
                search_term=payload.search_term.strip(),
                location=payload.location.strip() if payload.location else None,
                track=payload.track,
                results_wanted=payload.results_wanted or 50,
                site_name=payload.site_name or list(DEFAULT_SITE_NAMES),
                sort=(payload.sort or "focus"),
            )
        except ValueError as e:
            raise HttpError(400, str(e)) from e
        except RuntimeError as e:
            raise HttpError(502, str(e)) from e
        if session is not None:
            session["job_search_cache"] = {"params": cache_key, "refs": refs_for_cache}
            session.modified = True
        # Skip ranking block; use jobs_out_from_core and jump to display_limit / return
        display_limit = getattr(settings, "JOB_SEARCH_DISPLAY_LIMIT", 50)
        if payload.results_wanted is not None and payload.results_wanted > 0:
            display_limit = min(display_limit, payload.results_wanted)
        jobs_out = jobs_out_from_core[:display_limit]
        logger.info("[jobs/search] Returning %d jobs (from core, display_limit=%d)", len(jobs_out), display_limit)
        return JobSearchResponse(jobs=jobs_out, total=len(jobs_out))

    # Cache path: single ranking pipeline from job_search_core (preference + auto-dislike + penalty + sort)
    jobs_out = rank_and_filter_jobs(jobs_with_meta, payload.track or None)

    # Keep resume_id_for_match for AI Match step later
    resume_id_for_match = payload.resume_id
    if resume_id_for_match is None and jobs_out:
        latest = UserResume.objects.order_by("-uploaded_at").first()
        if latest:
            resume_id_for_match = latest.id

    display_limit = getattr(settings, "JOB_SEARCH_DISPLAY_LIMIT", 50)
    if payload.results_wanted is not None and payload.results_wanted > 0:
        display_limit = min(display_limit, payload.results_wanted)
    before_slice = len(jobs_out)
    jobs_out = jobs_out[:display_limit]
    logger.info("[jobs/search] Returning %d jobs (before slice=%d, display_limit=%d)", len(jobs_out), before_slice, display_limit)
    return JobSearchResponse(jobs=jobs_out, total=len(jobs_out))


# Session key for storing LLM match results (Why? page). Max keys to avoid huge session.
JOB_LLM_MATCH_SESSION_KEY = "job_llm_match"
JOB_LLM_MATCH_MAX_ENTRIES = 100


def _job_llm_match_session_key(job_listing_id: int, resume_id: int) -> str:
    return f"{job_listing_id}_{resume_id}"


@router.post("/ai-match", response=AiMatchResponse)
def jobs_ai_match(request, payload: AiMatchRequest):
    """Run LLM Matching for selected jobs (one at a time). Stores score+reasoning in session for Why? page."""
    if not payload.job_listing_ids:
        return AiMatchResponse(results=[], errors=[])
    resume = UserResume.objects.filter(id=payload.resume_id).first()
    if not resume or not resume.file:
        raise HttpError(400, "Resume not found or file missing.")
    resume_text = parse_pdf(resume.file.path) or ""
    if not resume_text:
        raise HttpError(400, "Could not extract text from resume.")
    try:
        llm = _get_llm_from_request(provider=payload.llm_provider, model=payload.llm_model)
    except HttpError:
        raise
    except Exception as e:
        raise HttpError(400, str(e)) from e
    prof = profile_for_llm(request)
    ms, mu, ml = resolve_prompt_parts(prof, "matching")
    from django.conf import settings
    max_jd_chars = getattr(settings, "JOB_MATCHING_JD_MAX_CHARS", 12000)
    resume_snippet = resume_text[:8000]
    job_map = {j.id: j for j in JobListing.objects.filter(id__in=payload.job_listing_ids)}
    results = []
    errors = []
    session = getattr(request, "session", None)
    stored = (session.get(JOB_LLM_MATCH_SESSION_KEY) or {}) if session else {}
    for jid in payload.job_listing_ids:
        job = job_map.get(jid)
        if not job or not (job.description or "").strip():
            errors.append(f"Job {jid}: no description")
            continue
        try:
            jd = (job.description or "")[:max_jd_chars]
            fmt_kw = {"resume_text": resume_snippet, "job_description": jd}
            try:
                pm = build_llm_messages_for_prompt(
                    legacy_combined=ml or None,
                    system_template=ms or None,
                    user_template=mu or None,
                    format_kwargs=fmt_kw,
                )
                prompt_text = "\n\n---\n\n".join(
                    f"{type(m).__name__}:\n{getattr(m, 'content', '')}" for m in pm
                )
            except Exception:
                prompt_text = None
            result = run_matching(
                resume_snippet,
                jd,
                llm,
                prompt_system=ms,
                prompt_user=mu,
                prompt_legacy=ml or None,
                job_cache_key=f"ai-match:{jid}:{payload.resume_id}",
            )
            score = result.get("score")
            reasoning = result.get("reasoning") or ""
            results.append(
                AiMatchResultItem(
                    job_listing_id=jid,
                    score=score or 0,
                    reasoning=reasoning,
                    provider=payload.llm_provider,
                    model=payload.llm_model,
                    prompt=prompt_text,
                )
            )
            if session:
                key = _job_llm_match_session_key(jid, payload.resume_id)
                stored[key] = {"score": score, "reasoning": reasoning}
        except Exception as e:
            logger.warning("AI match for job %s failed: %s", jid, e)
            errors.append(f"Job {jid}: {e}")
    if session and stored:
        # Trim to last N entries (by insertion order not guaranteed in dict; we keep all from this run + cap size)
        keys = list(stored.keys())
        if len(keys) > JOB_LLM_MATCH_MAX_ENTRIES:
            for k in keys[: len(keys) - JOB_LLM_MATCH_MAX_ENTRIES]:
                stored.pop(k, None)
        session[JOB_LLM_MATCH_SESSION_KEY] = stored
        session.modified = True
    return AiMatchResponse(results=results, errors=errors)


# Max total chars for concatenated job descriptions in Insights (avoid token overflow)
INSIGHTS_JD_MAX_TOTAL_CHARS = 50000


@router.post("/insights", response=InsightsResponse)
def jobs_insights(request, payload: InsightsRequest):
    """Run Insights prompt over concatenated descriptions of selected jobs; return LLM response in modal."""
    logger.info("[insights] Request: job_listing_ids=%s, llm_provider=%s, llm_model=%s", payload.job_listing_ids, payload.llm_provider, payload.llm_model)
    if not payload.job_listing_ids:
        raise HttpError(400, "job_listing_ids is required and must not be empty.")
    try:
        llm = _get_llm_from_request(provider=payload.llm_provider, model=payload.llm_model)
    except HttpError:
        raise
    except Exception as e:
        raise HttpError(400, str(e)) from e
    prof = profile_for_llm(request)
    is_sys, is_user, is_leg = resolve_prompt_parts(prof, "insights")
    job_map = {j.id: j for j in JobListing.objects.filter(id__in=payload.job_listing_ids)}
    parts = []
    total = 0
    for jid in payload.job_listing_ids:
        job = job_map.get(jid)
        if not job or not (job.description or "").strip():
            continue
        desc = (job.description or "").strip()
        title_line = f"[{job.title or 'Job'} @ {job.company_name or 'Company'}]"
        if total + len(title_line) + len(desc) + 10 > INSIGHTS_JD_MAX_TOTAL_CHARS:
            remaining = max(0, INSIGHTS_JD_MAX_TOTAL_CHARS - total - len(title_line) - 50)
            if remaining > 0:
                desc = desc[:remaining] + "\n...[truncated]"
            else:
                break
        parts.append(f"{title_line}\n{desc}")
        total += len(title_line) + len(desc) + 10
    job_descriptions = "\n\n---\n\n".join(parts) if parts else ""
    if not job_descriptions:
        raise HttpError(400, "No job descriptions found for the selected jobs.")
    try:
        messages = build_llm_messages_for_prompt(
            legacy_combined=is_leg or None,
            system_template=is_sys or None,
            user_template=is_user or None,
            format_kwargs={"job_descriptions": job_descriptions},
        )
        prompt_text = "\n\n---\n\n".join(
            f"{type(m).__name__}:\n{getattr(m, 'content', '')}" for m in messages
        )
    except Exception as e:
        logger.warning("Insights prompt format failed: %s", e)
        raise HttpError(400, str(e)) from e
    logger.info("[insights] Sending prompt to LLM (length=%d chars). First 300 chars: %s", len(prompt_text), (prompt_text[:300] + "..." if len(prompt_text) > 300 else prompt_text))
    try:
        raw = _llm_invoke_with_retry(
            llm,
            messages,
            job_cache_key="insights:" + ",".join(str(i) for i in payload.job_listing_ids),
            usage_query_kind=USAGE_QUERY_JOB_INSIGHTS,
        )
        content = raw.content if hasattr(raw, "content") else str(raw)
        if not isinstance(content, str):
            content = str(content)
    except LLMRequestsDisabled as e:
        raise HttpError(503, str(e)) from e
    except Exception as e:
        logger.warning("Insights LLM invoke failed: %s", e)
        raise HttpError(502, str(e)) from e
    return InsightsResponse(
        content=content,
        provider=payload.llm_provider,
        model=payload.llm_model,
        prompt=prompt_text,
    )


@router.get("/matches", response=List[JobMatchPayload])
def jobs_matches(request, resume_id: Optional[int] = None, min_score: Optional[int] = None, status: Optional[str] = None):
    """List analyzed jobs (JobMatchResult) with optional filters."""
    qs = JobMatchResult.objects.select_related("job_listing", "resume").order_by("-analyzed_at")
    if resume_id is not None:
        qs = qs.filter(resume_id=resume_id)
    if min_score is not None:
        qs = qs.filter(fit_score__gte=min_score)
    if status is not None:
        qs = qs.filter(status=status)

    out = []
    for m in qs:
        j = m.job_listing
        out.append(
            JobMatchPayload(
                job_listing_id=j.id,
                title=j.title,
                company_name=j.company_name,
                location=j.location or "",
                url=j.url or "",
                keyword=None,
                fit_score=m.fit_score,
                reasoning=m.reasoning or "",
                analyzed_at=m.analyzed_at.isoformat() if m.analyzed_at else "",
                status=m.status,
                resume_id=m.resume_id,
            )
        )
    return out


@router.post("/run-keyword-search", response=RunKeywordSearchResponse)
def jobs_run_keyword_search(request, payload: RunKeywordSearchRequest):
    """For each (keyword, resume_id): fetch jobs, then for each job without existing JobMatchResult run fit check and save."""
    if not payload.entries:
        raise HttpError(400, "entries is required (list of { keyword, resume_id })")
    location = (payload.location or "").strip() or None
    site_name = payload.site_name or list(DEFAULT_SITE_NAMES)
    results_wanted = payload.results_wanted or 50
    all_results = []
    errors = []

    for entry in payload.entries:
        keyword = (entry.keyword or "").strip()
        if not keyword:
            errors.append("Empty keyword skipped")
            continue
        resume_id = entry.resume_id
        try:
            resume = UserResume.objects.get(id=resume_id)
        except UserResume.DoesNotExist:
            errors.append(f"Resume id {resume_id} not found for keyword '{keyword}'")
            continue
        try:
            raw = fetch_jobs(
                search_term=keyword,
                location=location,
                site_name=site_name,
                results_wanted=results_wanted,
            )
        except Exception as e:
            errors.append(f"Fetch failed for '{keyword}': {e}")
            continue

        existing = set(
            JobMatchResult.objects.filter(resume_id=resume_id).values_list("job_listing_id", flat=True)
        )
        disliked_ids = set(
            JobListingAction.objects.filter(action=JobListingAction.ActionType.DISLIKED).values_list(
                "job_listing_id", flat=True
            )
        )
        disqualifier_pattern = build_disqualifier_pattern(get_disqualifier_phrases())
        candidates = []  # (job, resume, keyword)
        for r in raw:
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
            if job.id in existing or job.id in disliked_ids or job_matches_disqualifiers(job, disqualifier_pattern):
                continue
            candidates.append((job, resume, keyword))
            existing.add(job.id)

        # Reorder by hybrid focus score (title + role) using IC track by default.
        prefs = get_preference_vectors(track="ic")
        if prefs and candidates:
            try:
                jobs_for_rank = [j for j, _, _ in candidates]
                result = rank_jobs_by_preference(jobs_for_rank, track="ic")
                if result is None:
                    raise RuntimeError("rank_jobs_by_preference returned None")
                scores, _title_vecs, _role_vecs = result
                scored = list(zip(scores, candidates))
                scored.sort(key=lambda x: -x[0])
                candidates = [c for _, c in scored]
            except Exception as e:
                logger.warning("Preference reorder in keyword search failed: %s", e)

        try:
            resume_text = parse_pdf(resume.file.path)
        except Exception as e:
            errors.append(f"Resume PDF read failed: {e}")
            resume_text = None

        for job, res, kw in candidates:
            if resume_text is None:
                errors.append(f"Skipped job {job.id} (resume unreadable)")
                continue
            try:
                result = run_fit_check(
                    resume_text,
                    job.description or "",
                    None,
                    None,
                    job_cache_key=f"keyword-fit:{job.id}:{res.id}",
                    usage_query_kind=USAGE_QUERY_KEYWORD_SEARCH_FIT,
                )
            except LLMRequestsDisabled as e:
                errors.append(f"Job {job.id}: {e}")
                continue
            match_result, _ = JobMatchResult.objects.update_or_create(
                job_listing=job,
                resume=res,
                defaults={
                    "fit_score": result.get("score", 0),
                    "reasoning": result.get("reasoning", ""),
                    "status": JobMatchResult.STATUS_ANALYZED,
                },
            )
            all_results.append(
                JobMatchPayload(
                    job_listing_id=job.id,
                    title=job.title,
                    company_name=job.company_name,
                    location=job.location or "",
                    url=job.url or "",
                    keyword=kw,
                    fit_score=match_result.fit_score,
                    reasoning=match_result.reasoning or "",
                    thoughts=match_result.reasoning or "",
                    analyzed_at=match_result.analyzed_at.isoformat() if match_result.analyzed_at else "",
                    status=match_result.status,
                    resume_id=resume_id,
                )
            )

    return RunKeywordSearchResponse(results=all_results, errors=errors)


@router.get("/saved", response=JobSearchResponse)
def jobs_saved(request):
    """List saved (favourite) job listings."""
    saved = JobListingAction.objects.filter(
        action=JobListingAction.ActionType.SAVED
    ).select_related("job_listing").order_by("-created_at")
    jobs_out = []
    for s in saved:
        job = s.job_listing
        snippet = (job.description or "")[:300].replace("\n", " ")
        jobs_out.append(_job_to_payload(job, snippet=snippet))
    return JobSearchResponse(jobs=jobs_out, total=len(jobs_out))


@router.get("/disliked", response=JobSearchResponse)
def jobs_disliked(request):
    """List disliked job listings (excluded from search results)."""
    disliked = JobListingAction.objects.filter(
        action=JobListingAction.ActionType.DISLIKED
    ).select_related("job_listing").order_by("-created_at")
    jobs_out = []
    for d in disliked:
        job = d.job_listing
        snippet = (job.description or "")[:300].replace("\n", " ")
        jobs_out.append(_job_to_payload(job, snippet=snippet))
    return JobSearchResponse(jobs=jobs_out, total=len(jobs_out))
@router.post("/disqualifiers", response=DisqualifierPayload)
def disqualifiers_add(request, payload: DisqualifierAddRequest):
    """Add a disqualifier phrase (async-friendly). Jobs containing this phrase (word-boundary) are hidden."""
    phrase = (payload.phrase or "").strip()
    if not phrase or len(phrase) < 2:
        raise HttpError(400, "Phrase must be at least 2 characters.")
    norm = " ".join(phrase.lower().split())
    if not norm:
        raise HttpError(400, "Phrase is empty after normalizing.")
    obj, created = UserDisqualifier.objects.get_or_create(phrase=norm)
    return DisqualifierPayload(id=obj.id, phrase=obj.phrase)


@router.delete("/disqualifiers/{disqualifier_id}")
def disqualifiers_remove(request, disqualifier_id: int):
    """Remove a disqualifier by id (async-friendly)."""
    deleted, _ = UserDisqualifier.objects.filter(id=disqualifier_id).delete()
    if not deleted:
        raise HttpError(404, "Disqualifier not found.")
    return {"ok": True}


@router.get("/disqualifiers", response=List[DisqualifierPayload])
def disqualifiers_list(request):
    """List all disqualifier phrases (for syncing UI)."""
    return [DisqualifierPayload(id=d.id, phrase=d.phrase) for d in UserDisqualifier.objects.all().order_by("phrase")]
# Path-parameter routes last so they don't capture "search", "matches", "resumes", "run-keyword-search", "saved", "disliked"


@router.get("/focus-breakdown/{job_listing_id}")
def jobs_focus_breakdown(request, job_listing_id: int):
    """Return detailed focus score breakdown (title vs role) for debugging why a job matched."""
    data = get_focus_breakdown(job_listing_id)
    if data is None:
        raise HttpError(404, "Job not found or no preference data (like some jobs first).")
    return data


@router.get("/{job_listing_id}", response=JobDetailPayload)
def jobs_get(request, job_listing_id: int):
    """Get a single job listing (e.g. for pre-filling optimizer with job description)."""
    job = get_object_or_404(JobListing, id=job_listing_id)
    return JobDetailPayload(
        id=job.id,
        title=job.title,
        company_name=job.company_name,
        location=job.location or "",
        description=job.description or "",
        url=job.url or "",
        source=job.source,
    )


@router.post("/{job_listing_id}/match", response=MatchResponse)
def jobs_match(request, job_listing_id: int, payload: MatchRequest):
    """Run fit check for (job_listing, resume), save JobMatchResult, return score/reasoning/thoughts."""
    job = get_object_or_404(JobListing, id=job_listing_id)
    resume_id = payload.resume_id
    if not resume_id:
        latest = UserResume.objects.order_by("-uploaded_at").first()
        if not latest:
            raise HttpError(400, "No resume uploaded. Upload a resume first or pass resume_id.")
        resume_id = latest.id
    resume = get_object_or_404(UserResume, id=resume_id)

    llm = _get_llm_from_request(payload.llm_provider, payload.llm_model)
    try:
        resume_text = parse_pdf(resume.file.path)
    except Exception as e:
        raise HttpError(400, f"Could not read resume PDF: {e}") from e

    try:
        result = run_fit_check(
            resume_text,
            job.description or "",
            llm,
            None,
            job_cache_key=f"match:{job.id}:{resume.id}",
            usage_query_kind=USAGE_QUERY_JOBS_MATCH_API,
        )
    except LLMRequestsDisabled as e:
        raise HttpError(503, str(e)) from e
    score = result.get("score", 0)
    reasoning = result.get("reasoning", "")

    JobMatchResult.objects.update_or_create(
        job_listing=job,
        resume=resume,
        defaults={
            "fit_score": score,
            "reasoning": reasoning,
            "analyzed_at": timezone.now(),
            "status": JobMatchResult.STATUS_ANALYZED,
        },
    )
    return MatchResponse(
        score=score,
        reasoning=reasoning,
        job_listing_id=job.id,
        resume_id=resume.id,
    )


@router.post("/{job_listing_id}/mark-applied")
def jobs_mark_applied(request, job_listing_id: int, payload: MarkAppliedRequest):
    """Set JobMatchResult.status = applied for (job_listing, resume)."""
    job = get_object_or_404(JobListing, id=job_listing_id)
    resume = get_object_or_404(UserResume, id=payload.resume_id)
    updated = JobMatchResult.objects.filter(job_listing=job, resume=resume).update(
        status=JobMatchResult.STATUS_APPLIED, analyzed_at=timezone.now()
    )
    if not updated:
        raise HttpError(404, "No match result found for this job and resume. Run a fit check first.")
    return {"success": True}


@router.post("/{job_listing_id}/like")
def jobs_like(request, job_listing_id: int, track: Optional[str] = None):
    """Mark job as liked (preference signal). Store embedding for preference ranking. Optional track (ic/mgmt) overrides session."""
    job = get_object_or_404(JobListing, id=job_listing_id)
    session_track = (
        getattr(getattr(request, "session", None), "get", lambda *_: None)("job_search_track")
        if hasattr(getattr(request, "session", None), "get")
        else None
    )
    raw_track = _resolve_track(track, session_track)
    JobListingAction.objects.get_or_create(
        job_listing=job,
        action=JobListingAction.ActionType.LIKED,
        defaults={"track": raw_track},
    )
    JobListingAction.objects.filter(
        job_listing=job, action=JobListingAction.ActionType.DISLIKED
    ).delete()
    JobListingEmbedding.objects.filter(
        job_listing=job, embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED
    ).delete()
    vec = embedding_module.embed_job_text(job.title or "", job.description or "")
    if vec is not None:
        JobListingEmbedding.objects.update_or_create(
            job_listing=job,
            embedding_type=JobListingEmbedding.EmbeddingType.LIKED,
            track=raw_track,
            defaults={"embedding": vec, "track": raw_track},
        )
    invalidate_preference_cache()
    invalidate_disliked_embeddings_cache()
    return {"success": True}


@router.post("/{job_listing_id}/save")
def jobs_save(request, job_listing_id: int, track: Optional[str] = None):
    """Save job to favourites (saved list). Track-aware so saved jobs can be scoped to a track."""
    job = get_object_or_404(JobListing, id=job_listing_id)
    session_track = (
        getattr(getattr(request, "session", None), "get", lambda *_: None)("job_search_track")
        if hasattr(getattr(request, "session", None), "get")
        else None
    )
    raw_track = (track or session_track or "").strip().lower() or ""
    JobListingAction.objects.get_or_create(
        job_listing=job,
        action=JobListingAction.ActionType.SAVED,
        track=raw_track,
        defaults={"track": raw_track},
    )
    return {"success": True}


@router.post("/{job_listing_id}/unsave")
def jobs_unsave(request, job_listing_id: int):
    """Remove job from saved (favourites) list."""
    JobListingAction.objects.filter(
        job_listing_id=job_listing_id, action=JobListingAction.ActionType.SAVED
    ).delete()
    return {"success": True}


@router.post("/{job_listing_id}/dislike")
def jobs_dislike(request, job_listing_id: int, track: Optional[str] = None):
    """Mark job as disliked; it will be excluded from future search results. Optional track (ic/mgmt) overrides session."""
    job = get_object_or_404(JobListing, id=job_listing_id)
    session_track = (
        getattr(getattr(request, "session", None), "get", lambda *_: None)("job_search_track")
        if hasattr(getattr(request, "session", None), "get")
        else None
    )
    raw_track = _resolve_track(track, session_track)
    JobListingAction.objects.get_or_create(
        job_listing=job,
        action=JobListingAction.ActionType.DISLIKED,
        defaults={"track": raw_track},
    )
    JobListingAction.objects.filter(
        job_listing=job, action__in=[JobListingAction.ActionType.LIKED, JobListingAction.ActionType.SAVED]
    ).delete()
    JobListingEmbedding.objects.filter(job_listing=job).delete()
    vec = embedding_module.embed_full(job.title or "", job.description or "")
    if vec is not None:
        JobListingEmbedding.objects.update_or_create(
            job_listing=job,
            embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED,
            track=raw_track,
            defaults={"embedding": vec, "track": raw_track},
        )
    invalidate_preference_cache()
    invalidate_disliked_embeddings_cache()
    return {"success": True}
