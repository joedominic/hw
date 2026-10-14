"""
Career Profiler Views & APIs for the Resume-First Paradigm.
- career_profiler_view: Minimalist Robinhood-style UI for exploring career archetypes.
- career_profiler_analyze_api: Parses resume (existing or uploaded) and extracts archetypes.
- career_profiler_provision_api: Generates cleanly isolated SearchProfiles, Tracks, and Schedules.
"""
from __future__ import annotations

import json
import logging
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .models import UserResume, Track, SearchProfile, CareerProfileAnalysis
from .career_profiler import (
    extract_text_from_user_resume,
    analyze_resume_for_career_archetypes,
    provision_career_tracks,
)

logger = logging.getLogger(__name__)


@login_required
def career_profiler_view(request):
    """
    Renders the Robinhood-style minimalist Career Profiler page.
    Includes active master resumes, existing tracks, and entry flow.
    Retains and displays previously generated analyses without auto-running.
    """
    user = request.user
    resumes = list(UserResume.objects.for_user(user).order_by("-uploaded_at"))

    selected_resume_id = request.GET.get("resume_id")
    selected_resume = None
    if selected_resume_id:
        try:
            rid = int(selected_resume_id)
            for r in resumes:
                if r.id == rid:
                    selected_resume = r
                    break
        except (ValueError, TypeError):
            selected_resume = None

    if not selected_resume and resumes:
        selected_resume = resumes[0]

    existing_tracks = list(Track.objects.for_user(user).order_by("slug"))
    existing_profiles = list(SearchProfile.objects.for_user(user).order_by("name"))

    saved_analysis = None
    if selected_resume:
        record = (
            CareerProfileAnalysis.objects.for_user(user)
            .filter(resume=selected_resume)
            .order_by("-updated_at")
            .first()
        )
        if record and record.analysis_data:
            saved_analysis = record.analysis_data

    context = {
        "resumes": resumes,
        "selected_resume": selected_resume,
        "existing_tracks": existing_tracks,
        "existing_profiles": existing_profiles,
        "saved_analysis": saved_analysis,
        "saved_analysis_json": json.dumps(saved_analysis) if saved_analysis else "null",
        "active_nav": "profiler",
        "initial_flow": request.GET.get("mode", "profiler"),  # 'profiler' or 'fork'
    }
    return render(request, "resume_app/career_profiler.html", context)


@login_required
@require_http_methods(["POST"])
def career_profiler_analyze_api(request):
    """
    API endpoint: parses resume text and returns LLM/heuristic archetypes.
    Accepts:
      - Multipart upload with 'resume_file'
      - JSON with 'resume_id' or 'resume_text'
      - Form POST with 'resume_id'
    """
    user = request.user
    resume = None
    resume_text = ""

    # Check for direct file upload
    if request.FILES.get("resume_file"):
        uploaded_file = request.FILES["resume_file"]
        resume = UserResume.objects.create(
            owner=user,
            file=uploaded_file,
            original_filename=uploaded_file.name,
            is_library=True,
        )
        try:
            resume_text = extract_text_from_user_resume(resume)
        except Exception as e:
            logger.warning("[career_profiler_api] text extraction failed on upload: %s", e)
            return JsonResponse({"status": "error", "message": f"Could not read uploaded resume: {e}"}, status=400)

    # Check for JSON payload
    elif request.content_type == "application/json" or (request.body and not request.POST):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except Exception:
            payload = {}

        resume_id = payload.get("resume_id")
        raw_text = payload.get("resume_text")

        if resume_id:
            try:
                resume = UserResume.objects.for_user(user).get(id=int(resume_id))
                resume_text = extract_text_from_user_resume(resume)
            except UserResume.DoesNotExist:
                return JsonResponse({"status": "error", "message": "Specified resume not found."}, status=404)
            except Exception as e:
                logger.warning("[career_profiler_api] extraction failed for resume %s: %s", resume_id, e)
                return JsonResponse({"status": "error", "message": f"Could not read resume: {e}"}, status=400)
        elif raw_text:
            resume_text = str(raw_text).strip()

    # Fallback to standard POST data
    if not resume_text and request.POST.get("resume_id"):
        try:
            resume = UserResume.objects.for_user(user).get(id=int(request.POST["resume_id"]))
            resume_text = extract_text_from_user_resume(resume)
        except UserResume.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Specified resume not found."}, status=404)
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"Could not read resume: {e}"}, status=400)

    force_refresh = False
    if request.content_type == "application/json" or (request.body and not request.POST):
        try:
            payload = json.loads(request.body.decode("utf-8"))
            force_refresh = bool(payload.get("force_refresh"))
        except Exception:
            pass
    elif request.POST.get("force_refresh") in ("1", "true", "True"):
        force_refresh = True

    # If already analyzed for this resume and user didn't request a re-run, return stored analysis
    if not force_refresh and resume:
        stored = CareerProfileAnalysis.objects.for_user(user).filter(resume=resume).order_by("-updated_at").first()
        if stored and stored.analysis_data:
            return JsonResponse(stored.analysis_data)

    try:
        analysis = analyze_resume_for_career_archetypes(resume_text, user=user)
        cand_summary = analysis.get("candidate_summary") or {}
        detected_ind = cand_summary.get("domain") or analysis.get("detected_industry", "General")
        detected_sen = cand_summary.get("seniority_level") or analysis.get("detected_seniority", "Mid-Senior")
        core_skills = cand_summary.get("core_competencies") or analysis.get("core_skills", [])
        archetypes = analysis.get("archetypes", [])

        result_payload = {
            "status": "success",
            "resume_id": resume.id if resume else None,
            "resume_filename": (resume.original_filename or resume.file.name) if resume else "Direct Input",
            "detected_industry": detected_ind,
            "detected_seniority": detected_sen,
            "core_skills": core_skills,
            "archetypes": archetypes,
            "count": len(archetypes),
        }

        # Retain analysis in database so future page visits or switches do not re-run LLM
        if resume:
            try:
                CareerProfileAnalysis.objects.update_or_create(
                    owner=user,
                    resume=resume,
                    defaults={"analysis_data": result_payload},
                )
            except Exception as e:
                logger.warning("[career_profiler_api] could not cache analysis: %s", e)

        return JsonResponse(result_payload)
    except Exception as e:
        logger.exception("[career_profiler_api] archetype analysis failed: %s", e)
        return JsonResponse({"status": "error", "message": f"Analysis failed: {e}"}, status=500)


@login_required
@require_http_methods(["POST"])
def career_profiler_provision_api(request):
    """
    API endpoint: Provisions selected career tracks as isolated SearchProfiles,
    Tracks, and Scheduled Huey Scraper Tasks.
    """
    user = request.user
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    tracks_config = payload.get("tracks")
    if not tracks_config or not isinstance(tracks_config, list):
        return JsonResponse({"status": "error", "message": "At least one career track must be selected to provision."}, status=400)

    resume_id = payload.get("resume_id")
    try:
        created = provision_career_tracks(user, tracks_config, resume_id=resume_id)
        target_track = created[0]["slug"] if created else ""
        redirect_url = reverse("career_cockpit")
        if target_track:
            redirect_url += f"?track={target_track}"

        return JsonResponse({
            "status": "success",
            "created_tracks": created,
            "count": len(created),
            "redirect_url": redirect_url,
        })
    except Exception as e:
        logger.exception("[career_profiler_api] track provisioning failed: %s", e)
        return JsonResponse({"status": "error", "message": f"Failed to provision career tracks: {e}"}, status=500)


@login_required
@require_http_methods(["POST"])
def career_profiler_preview_matches_api(request):
    """
    Executes a live search preview for a suggested archetype profile using current
    fine-tuned criteria (query keywords, location, and site sources),
    and scores the results against the candidate's active resume.
    Enables the user to see how each suggested profile matches up with true searches
    and tune criteria before saving as a permanent SearchProfile.
    """
    import re
    from .job_sources import fetch_jobs, normalize_site_names, bulk_upsert_job_listings
    from .models import JobListing, UserDisqualifier
    from .disqualifiers import build_disqualifier_pattern, job_matches_disqualifiers
    from . import embeddings as embedding_module
    from .job_ranking import compute_title_textual_closeness, calibrate_desc_semantic_score

    user = request.user
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    search_term = (payload.get("search_term") or "").strip()
    if not search_term:
        return JsonResponse({"status": "error", "message": "Search term is required."}, status=400)

    location = (payload.get("location") or "").strip()
    site_names = payload.get("site_names") or ["indeed", "linkedin"]
    results_wanted = int(payload.get("results_wanted") or 10)
    resume_id = payload.get("resume_id")

    # Resolve candidate resume for matching
    resume = None
    if resume_id:
        try:
            resume = UserResume.objects.for_user(user).filter(pk=int(resume_id)).first()
        except Exception:
            resume = None
    if not resume:
        resume = UserResume.objects.for_user(user).filter(is_library=True).order_by("-uploaded_at").first()

    resume_text = ""
    if resume:
        try:
            resume_text = extract_text_from_user_resume(resume)
        except Exception as e:
            logger.warning("[career_profiler_preview] resume text extraction failed: %s", e)

    norm_sites = normalize_site_names(site_names)
    search_loc = location if location else None

    # Attempt live search via JobSpy / job_sources
    persisted: list[JobListing] = []
    try:
        raw_jobs = fetch_jobs(
            search_term=search_term,
            location=search_loc,
            site_name=norm_sites,
            results_wanted=max(5, min(results_wanted, 25)),
            hours_old=168,
        )
        if raw_jobs:
            persisted = bulk_upsert_job_listings(raw_jobs)
    except Exception as e:
        logger.info("[career_profiler_preview] live job fetch error or empty: %s", e)

    # If live scrapers return few or zero results, search existing JobListing database
    if len(persisted) < 5:
        existing_qs = JobListing.objects.filter(title__icontains=search_term)
        if search_loc:
            if search_loc.lower() == "remote":
                existing_qs = existing_qs.filter(location__icontains="remote")
            else:
                city_first = search_loc.split(",")[0].strip()
                if city_first:
                    loc_match = existing_qs.filter(location__icontains=city_first)
                    if loc_match.exists():
                        existing_qs = loc_match
        seen_ids = {j.id for j in persisted}
        for ej in existing_qs.order_by("-posted_at", "-fetched_at")[:15]:
            if ej.id not in seen_ids:
                persisted.append(ej)
                seen_ids.add(ej.id)

    # Disqualifier filter
    disqualifier_pattern = build_disqualifier_pattern(
        list(UserDisqualifier.objects.for_user(user).values_list("phrase", flat=True))
    )
    candidate_jobs: list[JobListing] = []
    for j in persisted:
        if disqualifier_pattern and job_matches_disqualifiers(j, disqualifier_pattern):
            continue
        candidate_jobs.append(j)

    # Pre-embed candidate resume once if available
    resume_vec = None
    if resume_text:
        try:
            resume_vec = embedding_module.embed_full("Resume", resume_text[:3000])
        except Exception as e:
            logger.warning("[career_profiler_preview] resume embed failed: %s", e)

    scored_matches = []
    for job in candidate_jobs:
        t_score, _ = compute_title_textual_closeness(
            candidate_title=job.title or "",
            target_terms=[search_term],
            liked_titles=[],
        )

        if resume_vec:
            try:
                job_vec = embedding_module.embed_full(job.title or "", job.description or "")
                if job_vec:
                    raw_desc_sim = embedding_module.cosine_similarity(job_vec, resume_vec)
                    d_score = calibrate_desc_semantic_score(raw_desc_sim)
                else:
                    d_score = 0.0
            except Exception:
                d_score = 0.0

            raw_hybrid = 0.40 * t_score + 0.60 * d_score
            # Title Guardrail
            if t_score < 0.15:
                final_score = min(raw_hybrid, t_score + 0.15)
            elif t_score < 0.30:
                final_score = min(raw_hybrid, t_score + 0.35)
            else:
                final_score = raw_hybrid
            match_pct = int(round(final_score * 100))
        else:
            match_pct = int(round(t_score * 100))

        date_str = ""
        if getattr(job, "posted_at", None):
            date_str = job.posted_at.strftime("%b %d")
        elif getattr(job, "fetched_at", None):
            date_str = job.fetched_at.strftime("%b %d")

        raw_desc = (job.description or "").strip()
        snippet = re.sub(r"\s+", " ", raw_desc)[:200]

        scored_matches.append({
            "id": job.id,
            "title": job.title or "Untitled Position",
            "company": job.company_name or "Unknown Company",
            "location": job.location or "Nationwide / Remote",
            "source": (job.source or "indeed").title(),
            "url": job.url or "",
            "date_posted": date_str,
            "match_score": max(5, min(99, match_pct)),
            "snippet": snippet,
        })

    scored_matches.sort(key=lambda x: -x["match_score"])

    return JsonResponse({
        "status": "success",
        "search_term": search_term,
        "location": location or "Nationwide (Open to Relocation)",
        "total_found": len(scored_matches),
        "matches": scored_matches[:12],
    })

