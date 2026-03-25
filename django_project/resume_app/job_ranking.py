from typing import List, Optional, Sequence, Tuple

import logging

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


def get_focus_breakdown(job_listing_id: int, track: Optional[str] = None) -> Optional[dict]:
    """
    Return detailed focus score breakdown for a job: title vs role (sentence-level) similarity
    vs preference and vs each liked job. Used by "Why?" debug view.
    """
    job = JobListing.objects.filter(id=job_listing_id).first()
    if not job:
        return None
    alpha = getattr(settings, "JOB_FOCUS_TITLE_WEIGHT", 0.55)
    top_k = getattr(settings, "JOB_FOCUS_ROLE_TOP_K", 5)
    prefs = get_preference_vectors(track=track)
    if not prefs:
        return None
    # prefs returns (liked_centroid, disliked_centroid, liked_jobs)
    pref_like_centroid = prefs[0]
    pref_dislike_centroid = prefs[1] if len(prefs) > 1 else None
    liked_jobs = (prefs[2] if len(prefs) > 2 else []) or get_liked_jobs_for_focus_reason(track=track)
    if not liked_jobs:
        return None
    tvec = embedding_module.embed_title_only(job.title or "", job.company_name or "")
    role_sent_vecs = embedding_module.get_role_sentence_vectors(job.title or "", job.description or "")
    # Full vector for this job (title + role-focused description) for per-liked/disliked comparison.
    full_vec = embedding_module.embed_full(job.title or "", job.description or "")
    if tvec is None or full_vec is None:
        return None
    # Overall similarity vs liked centroid: title and role both measured against like-centroid.
    title_sim = embedding_module.cosine_similarity(tvec, pref_like_centroid)
    role_sim = embedding_module.cosine_similarity(full_vec, pref_like_centroid)
    combined = gated_combined_score(title_sim, role_sim, alpha)
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
        disliked_embeddings = get_disliked_embeddings(track=track)
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
            "title_percent": _sim_to_percent(title_sim),
            "full_percent": _sim_to_percent(role_sim),
            "combined_percent": _sim_to_percent(combined),
        },
        "by_liked": by_liked[:10],
        "by_disliked": by_disliked[:10],
        "preference_margin": {
            "like_percent": like_percent,
            "dislike_percent": dislike_percent,
            "margin_percent": margin_percent,
        },
    }


def get_focus_sentence_alignment(job_listing_id: int, liked_job_id: int) -> Optional[dict]:
    """
    Sentence-level breakdown for one (job, liked_job) pair.
    Shows, for each role sentence in the job, the best-matching sentence from the liked job
    and whether it contributed to the top-k mean used for Role %.
    """
    job = JobListing.objects.filter(id=job_listing_id).first()
    liked_job = JobListing.objects.filter(id=liked_job_id).first()
    if not job or not liked_job:
        return None

    alpha = getattr(settings, "JOB_FOCUS_TITLE_WEIGHT", 0.55)
    top_k = getattr(settings, "JOB_FOCUS_ROLE_TOP_K", 5)

    t_job = embedding_module.embed_title_only(job.title or "", job.company_name or "")
    t_liked = embedding_module.embed_title_only(liked_job.title or "", liked_job.company_name or "")
    if t_job is None or t_liked is None:
        return None
    title_sim = embedding_module.cosine_similarity(t_job, t_liked)

    job_sentences = embedding_module.get_role_sentences(job.title or "", job.description or "")
    liked_sentences = embedding_module.get_role_sentences(liked_job.title or "", liked_job.description or "")
    if not job_sentences or not liked_sentences:
        return None

    try:
        job_vecs = embedding_module.embed_sentences_batch(job_sentences)
        liked_vecs = embedding_module.embed_sentences_batch(liked_sentences)
    except Exception:
        return None

    import numpy as np

    j_texts: list[str] = []
    j_vecs: list[np.ndarray] = []
    for s, v in zip(job_sentences, job_vecs):
        if v is not None:
            j_texts.append(s)
            j_vecs.append(np.array(v, dtype=float))
    l_texts: list[str] = []
    l_vecs: list[np.ndarray] = []
    for s, v in zip(liked_sentences, liked_vecs):
        if v is not None:
            l_texts.append(s)
            l_vecs.append(np.array(v, dtype=float))
    if not j_texts or not l_vecs:
        return None

    l_mat = np.stack(l_vecs, axis=0)
    l_norms = np.linalg.norm(l_mat, axis=1) + 1e-9

    sims_per_job: List[np.ndarray] = []
    max_sims: List[float] = []
    for j_vec in j_vecs:
        j_norm = float(np.linalg.norm(j_vec) + 1e-9)
        sims = np.dot(l_mat, j_vec) / (l_norms * j_norm)
        sims_per_job.append(sims)
        max_sims.append(float(np.max(sims)))

    if not max_sims:
        return None

    alignment_max_reuse = getattr(settings, "JOB_FOCUS_ALIGNMENT_LIKED_MAX_REUSE", 2)
    alignment_min_sim = getattr(settings, "JOB_FOCUS_ALIGNMENT_MIN_SIM", 0.55)
    liked_use_count = [0] * len(l_texts)
    assigned = {}
    for job_idx in sorted(range(len(j_texts)), key=lambda i: -max_sims[i]):
        sims = sims_per_job[job_idx]
        ranked = sorted(range(len(l_texts)), key=lambda l_idx: -float(sims[l_idx]))
        chosen_l_idx, chosen_sim = None, -1.0
        for l_idx in ranked:
            if liked_use_count[l_idx] < alignment_max_reuse:
                chosen_l_idx, chosen_sim = l_idx, float(sims[l_idx])
                liked_use_count[l_idx] += 1
                break
        if chosen_l_idx is None:
            chosen_l_idx, chosen_sim = int(np.argmax(sims)), float(np.max(sims))
        assigned[job_idx] = (chosen_l_idx, chosen_sim)

    per_sentence = []
    for i, s_text in enumerate(j_texts):
        l_idx, sim_val = assigned[i]
        below_threshold = sim_val < alignment_min_sim
        per_sentence.append(
            {
                "sentence": s_text,
                "best_liked_sentence": l_texts[l_idx],
                "similarity_percent": _sim_to_percent(sim_val),
                "below_threshold": below_threshold,
                "in_top_k": False,
            }
        )

    sorted_sims = sorted(max_sims, reverse=True)
    k = max(1, min(top_k, len(sorted_sims)))
    cutoff = sorted_sims[k - 1]
    for i, s_val in enumerate(max_sims):
        if s_val >= cutoff:
            per_sentence[i]["in_top_k"] = True

    role_sim = float(np.mean(sorted_sims[:k]))
    combined = gated_combined_score(title_sim, role_sim, alpha)

    indices_sorted = sorted(range(len(max_sims)), key=lambda i: -max_sims[i])
    rows_to_show = min(len(indices_sorted), max(10, k))
    sentences_for_ui = [per_sentence[i] for i in indices_sorted[:rows_to_show]]

    return {
        "job": {"id": job.id, "title": job.title or "", "company_name": job.company_name or ""},
        "liked_job": {
            "id": liked_job.id,
            "title": liked_job.title or "",
            "company_name": liked_job.company_name or "",
        },
        "alpha": alpha,
        "top_k": k,
        "title_percent": _sim_to_percent(title_sim),
        "role_percent": _sim_to_percent(role_sim),
        "combined_percent": _sim_to_percent(combined),
        "sentences": sentences_for_ui,
        "min_similarity_percent": _sim_to_percent(alignment_min_sim),
        "min_similarity_threshold": alignment_min_sim,
    }


def rank_jobs_by_preference(
    jobs: Sequence[JobListing],
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
    prefs = get_preference_vectors(track=track)
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

    scores: List[float] = []
    for tvec, fvec in zip(title_vecs, full_vecs):
        if tvec is None or fvec is None:
            scores.append(-1.0)
            continue
        try:
            title_sim = embedding_module.cosine_similarity(tvec, pref)
            full_sim = embedding_module.cosine_similarity(fvec, pref)
            score = gated_combined_score(title_sim, full_sim, alpha)
        except Exception:
            score = -1.0
        scores.append(score)

    return scores, title_vecs, full_vecs
