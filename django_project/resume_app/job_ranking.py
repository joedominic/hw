from typing import List, Optional, Sequence, Tuple, Dict, Set
import logging
import re

from django.conf import settings

from . import embeddings as embedding_module
from .models import JobListing
from .preference import get_preference_vectors, get_liked_jobs_for_focus_reason, get_disliked_embeddings

logger = logging.getLogger(__name__)


def _sim_to_percent(s: float) -> int:
    """Convert cosine sim in [-1,1] to 0-100."""
    return int(round(((max(-1, min(1, s)) + 1) / 2) * 100))


def gated_combined_score(title_sim: float, role_sim: float, alpha: float) -> float:
    """
    Combined = alpha * title_sim + (1-alpha) * role_sim, but when title_sim is below
    JOB_FOCUS_TITLE_GATE, cap combined so role text can't inflate cross-domain matches.
    """
    gate = getattr(settings, "JOB_FOCUS_TITLE_GATE", 0.30)
    max_lift = getattr(settings, "JOB_FOCUS_ROLE_MAX_LIFT", 0.15)
    raw = alpha * title_sim + (1 - alpha) * role_sim
    if title_sim < gate:
        raw = min(raw, title_sim + max_lift)
    return max(-1.0, min(1.0, raw))


def get_focus_breakdown(job_listing_id: int, *, user, track: Optional[str] = None) -> Optional[dict]:
    """
    Return detailed focus score breakdown for a job: title vs role (sentence-level) similarity
    vs preference and vs each liked job. Used by "Why?" debug view.
    """
    job = JobListing.objects.filter(id=job_listing_id).first()
    if not job:
        return None
    alpha = getattr(settings, "JOB_FOCUS_TITLE_WEIGHT", 0.55)
    top_k = getattr(settings, "JOB_FOCUS_ROLE_TOP_K", 5)
    prefs = get_preference_vectors(user=user, track=track)
    if not prefs:
        return None
    # prefs returns (liked_centroid, disliked_centroid, liked_jobs)
    pref_like_centroid = prefs[0]
    pref_dislike_centroid = prefs[1] if len(prefs) > 1 else None
    liked_jobs = (prefs[2] if len(prefs) > 2 else []) or get_liked_jobs_for_focus_reason(user=user, track=track)
    if not liked_jobs:
        return None
    tvec = embedding_module.embed_title_only(job.title or "", job.company_name or "")
    role_sent_vecs = embedding_module.get_role_sentence_vectors(job.title or "", job.description or "")
    # Full vector for this job (title + role-focused description) for per-liked/disliked comparison.
    full_vec = embedding_module.embed_full(job.title or "", job.description or "")
    if tvec is None or full_vec is None:
        return None
    # Compute V1 breakdown
    target_terms: List[str] = []
    if track:
        from .track_actions import normalize_track_slug
        slug = normalize_track_slug(track, user)
        target_terms.append(slug)
        from .models import SearchProfile
        sp = SearchProfile.objects.filter(owner=user, slug=slug).first()
        if sp:
            if sp.search_term:
                target_terms.append(sp.search_term)
            if sp.name and sp.name not in target_terms:
                target_terms.append(sp.name)
    liked_titles = [ltitle for _, ltitle, _, _, _ in (liked_jobs or [])]
    v1_res = compute_v1_job_score(
        job,
        target_terms=target_terms,
        liked_titles=liked_titles,
        liked_centroid=pref_like_centroid,
    )

    by_liked = []
    for (lid, ltitle, lcompany, ltvec, lrole_vecs) in liked_jobs:
        # Title similarity: title-only vectors.
        ts = embedding_module.cosine_similarity(tvec, ltvec) if (tvec is not None and ltvec is not None) else 0.0
        # Role similarity per liked job: use full-job embeddings when available so an identical
        # job and liked job show ~100% match, instead of falling back to a neutral 50%.
        if full_vec is not None and ltvec is not None:
            rs = embedding_module.cosine_similarity(full_vec, ltvec)
        else:
            # Fallback: if we ever start storing per-liked role sentence vectors again.
            rs = embedding_module.role_similarity_topk_mean(role_sent_vecs, lrole_vecs, top_k)
        comb = gated_combined_score(ts, rs, alpha)
        by_liked.append(
            {
                "id": lid,
                "title": ltitle,
                "company_name": lcompany,
                "title_percent": _sim_to_percent(ts),
                "full_percent": _sim_to_percent(rs),
                "combined_percent": _sim_to_percent(comb),
            }
        )
    by_liked.sort(key=lambda x: -x["combined_percent"])

    # --- Disliked jobs breakdown: how similar this job is to jobs you've disliked ---
    by_disliked = []
    try:
        disliked_embeddings = get_disliked_embeddings(user=user, track=track)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[focus_breakdown] get_disliked_embeddings failed: %s", e)
        disliked_embeddings = []
    if disliked_embeddings and full_vec is not None:
        for jid, d_vec in disliked_embeddings:
            try:
                d_job = JobListing.objects.filter(id=jid).first()
                if not d_job or not d_vec:
                    continue
                jt = embedding_module.embed_title_only(job.title or "", job.company_name or "") or full_vec
                dt = embedding_module.embed_title_only(d_job.title or "", d_job.company_name or "") or d_vec
                ts = embedding_module.cosine_similarity(jt, dt)
                rs = embedding_module.cosine_similarity(full_vec, d_vec)
                comb = gated_combined_score(ts, rs, alpha)
                by_disliked.append(
                    {
                        "id": d_job.id,
                        "title": d_job.title or "",
                        "company_name": d_job.company_name or "",
                        "title_percent": _sim_to_percent(ts),
                        "full_percent": _sim_to_percent(rs),
                        "combined_percent": _sim_to_percent(comb),
                    }
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("[focus_breakdown] error computing disliked similarity for job %s: %s", jid, e)
        by_disliked.sort(key=lambda x: -x["combined_percent"])

    # --- Additional metric: Like–Dislike margin (centroid-based, experimental) ---
    like_sim = embedding_module.cosine_similarity(full_vec, pref_like_centroid)
    like_percent = _sim_to_percent(like_sim)
    if pref_dislike_centroid is not None:
        dislike_sim = embedding_module.cosine_similarity(full_vec, pref_dislike_centroid)
        dislike_percent = _sim_to_percent(dislike_sim)
    else:
        dislike_sim = 0.0
        dislike_percent = 0
    margin_percent = like_percent - dislike_percent

    return {
        "job": {"id": job.id, "title": job.title or "", "company_name": job.company_name or ""},
        "alpha": alpha,
        "overall": {
            "title_percent": int(round(v1_res["title_textual_pct"])),
            "full_percent": int(round(v1_res["desc_calibrated_pct"])),
            "combined_percent": v1_res["final_pct"],
        },
        "by_liked": by_liked[:10],
        "by_disliked": by_disliked[:10],
        "preference_margin": {
            "like_percent": like_percent,
            "dislike_percent": dislike_percent,
            "margin_percent": margin_percent,
        },
    }


def rank_jobs_by_preference(
    jobs: Sequence[JobListing],
    *,
    user,
    track: Optional[str] = None,
) -> Optional[Tuple[List[float], List[Optional[List[float]]], List[List[Optional[List[float]]]]]]:
    """
    Shared helper: given a sequence of JobListing, compute hybrid preference scores.

    We now follow the “description as a whole” approach:
    - title_vecs: title-only embeddings from embed_title_only_batch
    - full_vecs: full job embeddings (title + role-focused description slice)
    Preference vectors are built in the same full-embedding space.

    Returns a tuple of (scores, title_vecs, full_vecs) where:
    - scores: combined focus scores (one per job, in same order as input)
    - title_vecs: embedding vectors for titles/companies
    - full_vecs: full-description embedding vectors per job.

    On failure or when preference data is missing, returns None.
    """
    prefs = get_preference_vectors(user=user, track=track)
    if not prefs or not jobs:
        return None

    alpha = getattr(settings, "JOB_FOCUS_TITLE_WEIGHT", 0.55)
    pref = prefs[0]

    title_batch = [(j.title or "", j.company_name or "") for j in jobs]
    full_batch = [(j.title or "", j.description or "") for j in jobs]

    try:
        title_vecs = embedding_module.embed_title_only_batch(title_batch)
        full_vecs = embedding_module.embed_full_batch(full_batch)
    except Exception as e:
        logger.warning("Preference embedding batch failed, returning None: %s", e)
        return None

    target_terms: List[str] = []
    if track:
        from .track_actions import normalize_track_slug
        slug = normalize_track_slug(track, user)
        target_terms.append(slug)
        from .models import SearchProfile
        sp = SearchProfile.objects.filter(owner=user, slug=slug).first()
        if sp:
            if sp.search_term:
                target_terms.append(sp.search_term)
            if sp.name and sp.name not in target_terms:
                target_terms.append(sp.name)

    liked_jobs = (prefs[2] if len(prefs) > 2 else []) or get_liked_jobs_for_focus_reason(user=user, track=track)
    liked_titles = [ltitle for _, ltitle, _, _, _ in (liked_jobs or [])]

    alpha_title = getattr(settings, "JOB_FOCUS_V1_TITLE_WEIGHT", 0.35)
    alpha_desc = getattr(settings, "JOB_FOCUS_V1_DESC_WEIGHT", 0.65)

    scores: List[float] = []
    for job, tvec, fvec in zip(jobs, title_vecs, full_vecs):
        if tvec is None or fvec is None:
            scores.append(-1.0)
            continue
        try:
            # 1. Title Textual Closeness
            t_score, _ = compute_title_textual_closeness(job.title or "", target_terms, liked_titles)
            # 2. Calibrated Description Semantic Closeness
            raw_desc_sim = embedding_module.cosine_similarity(fvec, pref)
            d_score = calibrate_desc_semantic_score(raw_desc_sim)
            # 3. Base Hybrid
            raw_hybrid = alpha_title * t_score + alpha_desc * d_score
            # 4. Title Guardrail
            if t_score < 0.15:
                final_hybrid = min(raw_hybrid, t_score + 0.15)
            elif t_score < 0.30:
                final_hybrid = min(raw_hybrid, t_score + 0.35)
            else:
                final_hybrid = raw_hybrid
            final_pct = int(round(final_hybrid * 100))
            # Map [0, 100] percent to [-1.0, 1.0] float for downstream consistency
            score = (final_pct / 50.0) - 1.0
        except Exception:
            score = -1.0
        scores.append(score)

    return scores, title_vecs, full_vecs


# =====================================================================
# V1 HYBRID SCORING IMPLEMENTATION
# Lexical / Textual Closeness for Titles + Calibrated Semantic for Descs
# =====================================================================

TITLE_STOPWORDS: Set[str] = {
    "and", "or", "the", "in", "at", "for", "of", "to", "a", "an", "with",
    "on", "by", "as", "hybrid", "remote", "full", "time", "part", "location",
    "us", "usa", "sr", "jr", "ii", "iii", "iv", "level", "team",
}


def normalize_title_token(t: str) -> str:
    """
    Lightweight, industry-agnostic English suffix normalizer.
    Unifies word forms (e.g. architect/architecture, develop/developer/development,
    engineer/engineering, intern/internship, manage/manager/management).
    """
    t = t.lower().strip()
    suffixes = [
        "ships", "ship", "ation", "ations", "ition", "itions",
        "ing", "ings", "ment", "ments", "ance", "ence",
        "ers", "er", "ors", "or", "ies", "es", "ed", "al", "ic", "s",
    ]
    for sfx in suffixes:
        if len(t) > len(sfx) + 3 and t.endswith(sfx):
            return t[:-len(sfx)]
    return t


def tokenize_title(text: str) -> Set[str]:
    """Tokenize a title string into normalized, meaningful tokens."""
    if not text:
        return set()
    raw_tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    tokens = set()
    for tok in raw_tokens:
        if tok not in TITLE_STOPWORDS and len(tok) > 1 and not tok.isdigit():
            tokens.add(normalize_title_token(tok))
    return tokens


def compute_title_textual_closeness(
    candidate_title: str,
    target_terms: Sequence[str],
    liked_titles: Sequence[str],
) -> Tuple[float, dict]:
    """
    Computes lexical / textual closeness [0.0, 1.0] of a candidate title
    against target search terms and user's liked titles.
    Industry-agnostic: relies on keyword containment, token recall, and profile domain coverage.
    """
    cand_tokens = tokenize_title(candidate_title)
    if not cand_tokens:
        return 0.0, {"reason": "empty_candidate"}

    # 1. Match against target search terms (e.g., 'software architect')
    best_target_score = 0.0
    for term in target_terms:
        tgt_tokens = tokenize_title(term)
        if not tgt_tokens:
            continue
        overlap = cand_tokens & tgt_tokens
        if not overlap:
            continue
        recall = len(overlap) / len(tgt_tokens)
        jaccard = len(overlap) / len(cand_tokens | tgt_tokens)
        score = 0.70 * recall + 0.30 * jaccard
        
        # Check if head noun (last token of target phrase) is matched in multi-token targets
        raw_words = [w for w in term.lower().split() if w]
        if len(tgt_tokens) > 1 and raw_words:
            head_tok = normalize_title_token(raw_words[-1])
            if head_tok not in overlap:
                score *= 0.50  # modifier-only overlap (e.g. 'software' without 'architect')
        if score > best_target_score:
            best_target_score = score

    # 2. Match against user's liked job titles and profile vocabulary
    best_liked_score = 0.0
    best_liked_title = None
    all_liked_tokens: Set[str] = set()
    for lt in liked_titles:
        lt_tokens = tokenize_title(lt)
        if not lt_tokens:
            continue
        all_liked_tokens.update(lt_tokens)
        overlap = cand_tokens & lt_tokens
        if not overlap:
            continue
        recall = len(overlap) / len(lt_tokens)
        jaccard = len(overlap) / len(cand_tokens | lt_tokens)
        score = 0.65 * recall + 0.35 * jaccard
        if score > best_liked_score:
            best_liked_score = score
            best_liked_title = lt

    # Domain vocabulary coverage
    domain_overlap = cand_tokens & all_liked_tokens
    cand_coverage = len(domain_overlap) / len(cand_tokens) if cand_tokens else 0.0

    liked_composite = 0.50 * best_liked_score + 0.50 * cand_coverage

    if target_terms:
        if best_target_score > 0.0:
            raw_title_score = max(best_target_score, 0.60 * best_target_score + 0.40 * liked_composite)
        else:
            # Candidate missed search profile target completely
            raw_title_score = 0.50 * liked_composite
    else:
        raw_title_score = liked_composite

    details = {
        "best_target_score": round(best_target_score, 3),
        "best_liked_score": round(best_liked_score, 3),
        "best_liked_title": best_liked_title,
        "cand_coverage": round(cand_coverage, 3),
    }
    return round(raw_title_score, 4), details


def calibrate_desc_semantic_score(raw_cosine: Optional[float]) -> float:
    """
    Calibrate raw cosine similarity to [0.0, 1.0].
    Removes the linear 50% floor of (cosine + 1)/2.
    Baseline unaligned English job text is ~0.25-0.30.
    Strong semantic alignment is >= 0.75-0.80.
    """
    if raw_cosine is None:
        return 0.0
    calibrated = (raw_cosine - 0.25) / (0.80 - 0.25)
    return max(0.0, min(1.0, calibrated))


def compute_v1_job_score(
    job: JobListing,
    *,
    target_terms: Sequence[str],
    liked_titles: Sequence[str],
    liked_centroid: List[float],
    alpha_title: float = 0.35,
    alpha_desc: float = 0.65,
) -> dict:
    """
    Hybrid V1 Scoring for a single JobListing:
    - Title: Textual closeness (Token recall + Jaccard + profile domain coverage)
    - Description: Semantic closeness (Calibrated sentence embedding against liked centroid)
    - Weights: 35% Title Textual, 65% Description Semantic
    - Guardrail: Title Gate caps description inflation if title textual closeness is weak.
    """
    t_score, t_details = compute_title_textual_closeness(job.title or "", target_terms, liked_titles)

    full_vec = embedding_module.embed_full(job.title or "", job.description or "")
    if full_vec is not None and liked_centroid is not None:
        raw_desc_sim = embedding_module.cosine_similarity(full_vec, liked_centroid)
        d_score = calibrate_desc_semantic_score(raw_desc_sim)
    else:
        raw_desc_sim = 0.0
        d_score = 0.0

    raw_hybrid = alpha_title * t_score + alpha_desc * d_score

    # Title Guardrail
    if t_score < 0.15:
        final_score = min(raw_hybrid, t_score + 0.15)
    elif t_score < 0.30:
        final_score = min(raw_hybrid, t_score + 0.35)
    else:
        final_score = raw_hybrid

    final_pct = int(round(final_score * 100))
    return {
        "final_pct": final_pct,
        "title_textual_pct": round(t_score * 100, 1),
        "desc_calibrated_pct": round(d_score * 100, 1),
        "raw_desc_sim": round(raw_desc_sim, 3),
        "t_details": t_details,
    }


def rank_jobs_by_preference_v1(
    jobs: Sequence[JobListing],
    *,
    user,
    track: Optional[str] = None,
    target_search_term: Optional[str] = None,
    alpha_title: float = 0.35,
    alpha_desc: float = 0.65,
) -> Optional[Tuple[List[int], List[dict]]]:
    """
    Batch ranking using V1 Hybrid Scoring.
    Returns (v1_percent_scores, detailed_results).
    """
    prefs = get_preference_vectors(user=user, track=track)
    if not prefs or not jobs:
        return None

    liked_centroid = prefs[0]
    liked_jobs = (prefs[2] if len(prefs) > 2 else []) or get_liked_jobs_for_focus_reason(user=user, track=track)
    liked_titles = [ltitle for _, ltitle, _, _, _ in (liked_jobs or [])]

    target_terms = []
    if target_search_term:
        target_terms.append(target_search_term)
    if track:
        target_terms.append(track)

    results: List[dict] = []
    scores: List[int] = []
    for job in jobs:
        res = compute_v1_job_score(
            job,
            target_terms=target_terms,
            liked_titles=liked_titles,
            liked_centroid=liked_centroid,
            alpha_title=alpha_title,
            alpha_desc=alpha_desc,
        )
        scores.append(res["final_pct"])
        results.append(res)

    return scores, results

