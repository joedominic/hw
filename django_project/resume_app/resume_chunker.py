"""
Hierarchical section-aware resume chunker and hybrid RAG retrieval service for HireEdge.

Transforms unstructured resume text into context-prepended semantic chunks:
- Extracts structured sections: Experience, Skills, Education, Projects, Summary.
- Injects parent metadata [Role @ Company | Dates] into each bullet cluster.
- Computes and caches dense unit embeddings (all-MiniLM-L6-v2 via ONNX INT8).
- Persists to ResumeChunk database model (Phase 2 RAG).
- Provides hybrid dense (75%) + BM25 (25%) retrieval for LLM Resume Optimizer.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.conf import settings
from django.db import transaction

from .models import ResumeChunk, UserResume

logger = logging.getLogger(__name__)

# Section identification patterns (case-insensitive)
_SECTION_PATTERNS = [
    (r"^(?:professional\s+)?experience|work\s+history|employment|work\s+experience|career\s+history", "EXPERIENCE"),
    (r"^technical\s+skills|skills\s*(?:&|and)\s*competencies|core\s+competencies|technologies|skills", "SKILLS"),
    (r"^education|academic\s+background|degrees", "EDUCATION"),
    (r"^projects|key\s+projects|selected\s+projects", "PROJECTS"),
    (r"^certifications|certificates|licenses", "CERTIFICATIONS"),
    (r"^summary|professional\s+summary|executive\s+summary|profile|about\s+me", "SUMMARY"),
]

# Bullet start indicators
_BULLET_PREFIX_RE = re.compile(r"^(?:[\-\*\•\u2022\u25cf\u2043\u2219]|(?:\d+[\.\)]))\s*")

# Date range pattern for experience headers
_DATE_RANGE_RE = re.compile(
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?\d{4}\s*[-–—to\s]+\s*(?:(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?\d{4}|present|current)",
    re.IGNORECASE,
)


class ResumeChunker:
    """
    Parses resume text into logical, context-enriched chunks.
    Ensures that every chunk has parent metadata attached (Company, Role, Dates, Section)
    so dense embeddings represent the full semantic context.
    """

    @classmethod
    def chunk(cls, text: str) -> List[Dict[str, str]]:
        """
        Parse resume into a list of chunk dicts:
        [{ "section": "EXPERIENCE", "text": "[Experience | Senior Eng @ Acme (2020-2023)] ..." }, ...]
        """
        if not text or not text.strip():
            return []

        lines = [ln.strip() for ln in text.split("\n")]
        sections = cls._segment_sections(lines)
        chunks: List[Dict[str, str]] = []

        for sec_name, sec_lines in sections:
            if not sec_lines:
                continue

            if sec_name == "EXPERIENCE":
                chunks.extend(cls._chunk_experience(sec_lines))
            elif sec_name == "SKILLS":
                chunks.extend(cls._chunk_skills(sec_lines))
            elif sec_name == "EDUCATION":
                chunks.extend(cls._chunk_education(sec_lines))
            elif sec_name == "PROJECTS":
                chunks.extend(cls._chunk_projects(sec_lines))
            else:
                chunks.extend(cls._chunk_generic(sec_name, sec_lines))

        # Fallback if no sections were recognized (e.g. flat plain text)
        if not chunks:
            chunks = cls._chunk_generic("GENERAL", lines)

        return chunks

    @classmethod
    def _segment_sections(cls, lines: List[str]) -> List[Tuple[str, List[str]]]:
        """Group lines by top-level section header."""
        sections: List[Tuple[str, List[str]]] = []
        current_section = "SUMMARY"
        current_lines: List[str] = []

        header_regexes = [(re.compile(pat, re.IGNORECASE), name) for pat, name in _SECTION_PATTERNS]

        for line in lines:
            if not line:
                continue

            # Check if line looks like a major section heading:
            # Short length (< 45 chars) and matches one of the known section patterns
            clean_line = re.sub(r"^[\#\*\_\-]+|\:+$", "", line).strip()
            if len(clean_line) < 45:
                matched_sec = None
                for regex, sec_name in header_regexes:
                    if regex.search(clean_line):
                        matched_sec = sec_name
                        break
                if matched_sec:
                    if current_lines:
                        sections.append((current_section, current_lines))
                        current_lines = []
                    current_section = matched_sec
                    continue

            current_lines.append(line)

        if current_lines:
            sections.append((current_section, current_lines))

        return sections

    @classmethod
    def _chunk_experience(cls, lines: List[str]) -> List[Dict[str, str]]:
        """
        Chunk experience section. Detects job role headers and attaches
        [Role @ Company | Dates] context to each bullet group.
        """
        chunks: List[Dict[str, str]] = []
        current_context = ""
        current_bullets: List[str] = []

        def flush_bullets():
            nonlocal current_bullets
            if not current_bullets:
                return
            # Bundle 1-3 bullets together to keep chunk size optimal (50-150 words)
            cluster: List[str] = []
            cluster_len = 0
            for b in current_bullets:
                b_clean = _BULLET_PREFIX_RE.sub("", b).strip()
                if not b_clean:
                    continue
                cluster.append(b_clean)
                cluster_len += len(b_clean.split())
                if cluster_len >= 50:
                    text_body = " ".join(cluster)
                    chunk_text = f"[{current_context}] {text_body}" if current_context else text_body
                    chunks.append({"section": "EXPERIENCE", "text": chunk_text})
                    cluster = []
                    cluster_len = 0

            if cluster:
                text_body = " ".join(cluster)
                chunk_text = f"[{current_context}] {text_body}" if current_context else text_body
                chunks.append({"section": "EXPERIENCE", "text": chunk_text})
            current_bullets = []

        for line in lines:
            is_bullet = bool(_BULLET_PREFIX_RE.match(line))
            date_match = _DATE_RANGE_RE.search(line)

            # If line is not a bullet and has dates, or is a strong candidate for Role / Company
            if not is_bullet and (date_match or (len(line) < 80 and any(sep in line for sep in ["|", "–", "—", "-", ","]))):
                flush_bullets()
                clean_hdr = re.sub(r"[\#\*\_\-]+", "", line).strip()
                current_context = f"Experience | {clean_hdr}"
            elif is_bullet:
                current_bullets.append(line)
            else:
                # Narrative line or sub-bullet
                if current_bullets:
                    current_bullets[-1] += " " + line
                else:
                    current_bullets.append(line)

        flush_bullets()
        return chunks

    @classmethod
    def _chunk_skills(cls, lines: List[str]) -> List[Dict[str, str]]:
        """Chunk skills section into categorized clusters."""
        chunks: List[Dict[str, str]] = []
        cluster: List[str] = []
        cluster_words = 0

        for line in lines:
            clean = _BULLET_PREFIX_RE.sub("", line).strip()
            if not clean:
                continue
            cluster.append(clean)
            cluster_words += len(clean.split())
            if cluster_words >= 35:
                text = " ".join(cluster)
                chunks.append({"section": "SKILLS", "text": f"[Skills] {text}"})
                cluster = []
                cluster_words = 0

        if cluster:
            text = " ".join(cluster)
            chunks.append({"section": "SKILLS", "text": f"[Skills] {text}"})

        return chunks

    @classmethod
    def _chunk_education(cls, lines: List[str]) -> List[Dict[str, str]]:
        """Chunk education entries."""
        chunks: List[Dict[str, str]] = []
        entry: List[str] = []

        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            entry.append(clean)
            if len(entry) >= 2 or len(" ".join(entry).split()) >= 30:
                chunks.append({"section": "EDUCATION", "text": f"[Education] {' '.join(entry)}"})
                entry = []

        if entry:
            chunks.append({"section": "EDUCATION", "text": f"[Education] {' '.join(entry)}"})
        return chunks

    @classmethod
    def _chunk_projects(cls, lines: List[str]) -> List[Dict[str, str]]:
        """Chunk projects section with [Project] context."""
        chunks: List[Dict[str, str]] = []
        current_project = ""
        current_bullets: List[str] = []

        def flush():
            nonlocal current_bullets
            if current_bullets:
                body = " ".join(_BULLET_PREFIX_RE.sub("", b).strip() for b in current_bullets)
                ctx = f"Project: {current_project}" if current_project else "Projects"
                chunks.append({"section": "PROJECTS", "text": f"[{ctx}] {body}"})
                current_bullets = []

        for line in lines:
            is_bullet = bool(_BULLET_PREFIX_RE.match(line))
            if not is_bullet and len(line) < 60:
                flush()
                current_project = re.sub(r"[\#\*\_\-]+", "", line).strip()
            else:
                current_bullets.append(line)

        flush()
        return chunks

    @classmethod
    def _chunk_generic(cls, section_name: str, lines: List[str]) -> List[Dict[str, str]]:
        """Generic paragraph/sentence chunker for summary, certifications, etc."""
        chunks: List[Dict[str, str]] = []
        cluster: List[str] = []
        cluster_words = 0

        for line in lines:
            clean = _BULLET_PREFIX_RE.sub("", line).strip()
            if not clean:
                continue
            cluster.append(clean)
            cluster_words += len(clean.split())
            if cluster_words >= 50:
                body = " ".join(cluster)
                chunks.append({"section": section_name, "text": f"[{section_name}] {body}"})
                cluster = []
                cluster_words = 0

        if cluster:
            body = " ".join(cluster)
            chunks.append({"section": section_name, "text": f"[{section_name}] {body}"})

        return chunks


def compute_resume_fingerprint(text: str) -> str:
    """Deterministic hash of clean resume text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def sync_resume_chunks(user_resume: UserResume, text: Optional[str] = None) -> List[ResumeChunk]:
    """
    Ensure ResumeChunk rows exist and are synchronized with the user's resume text.
    Idempotent: uses source_fingerprint to avoid re-embedding unchanged resumes.
    """
    if text is None:
        from .career_profiler import extract_text_from_user_resume
        try:
            text = extract_text_from_user_resume(user_resume)
        except Exception as e:
            logger.warning("[sync_resume_chunks] failed to extract text from resume %s: %s", user_resume.id, e)
            return list(user_resume.resume_chunks.all())

    if not text or not text.strip():
        return []

    fingerprint = compute_resume_fingerprint(text)

    # Check if up-to-date chunks already exist
    existing = list(user_resume.resume_chunks.filter(source_fingerprint=fingerprint).order_by("ordinal"))
    if existing:
        return existing

    from . import embeddings as embedding_module

    # Parse into context-enriched chunks
    raw_chunks = ResumeChunker.chunk(text)
    if not raw_chunks:
        return []

    chunk_texts = [c["text"] for c in raw_chunks]
    # Fast length-sorted batch embedding via ONNX INT8
    embeddings = embedding_module.embed_sentences_batch(chunk_texts)

    to_create: List[ResumeChunk] = []
    for idx, (c, emb) in enumerate(zip(raw_chunks, embeddings)):
        if emb is None:
            continue
        to_create.append(
            ResumeChunk(
                user_resume=user_resume,
                ordinal=idx,
                section=c["section"][:64],
                text=c["text"],
                embedding=emb,
                embedding_model=embedding_module._MODEL_NAME,
                source_fingerprint=fingerprint,
            )
        )

    if not to_create:
        return []

    with transaction.atomic():
        user_resume.resume_chunks.all().delete()
        created = ResumeChunk.objects.bulk_create(to_create)

    logger.info(
        "[sync_resume_chunks] indexed resume %s: %d chunks created (fingerprint=%s)",
        user_resume.id,
        len(created),
        fingerprint,
    )
    return created


def _tokenize_for_bm25(text: str) -> List[str]:
    """Lowercase tokenization for BM25 matching."""
    return [w for w in re.split(r"[^\w]+", (text or "").lower()) if len(w) >= 2]


def retrieve_relevant_chunks(
    user_resume: UserResume,
    query_text: str,
    top_k: int = 8,
    dense_weight: float = 0.75,
    keyword_weight: float = 0.25,
) -> List[Dict[str, Any]]:
    """
    Hybrid dense (embedding cosine) + sparse (BM25 keyword) retrieval over ResumeChunk rows.
    Returns the top_k most relevant chunks for the target query.
    """
    if not query_text or not query_text.strip():
        return []

    chunks = sync_resume_chunks(user_resume)
    if not chunks:
        return []

    from . import embeddings as embedding_module

    # 1. Dense scoring
    query_vec = embedding_module.embed_full("Target Role", query_text)
    dense_scores: List[float] = [0.0] * len(chunks)

    if query_vec:
        for idx, chunk in enumerate(chunks):
            c_emb = chunk.embedding
            if c_emb:
                # Cosine similarity in [-1, 1], normalized to [0, 1]
                sim = embedding_module.cosine_similarity(query_vec, c_emb)
                dense_scores[idx] = max(0.0, min(1.0, (sim + 1.0) / 2.0))

    # 2. BM25 keyword scoring
    bm25_norm_scores: List[float] = [0.0] * len(chunks)
    if keyword_weight > 0:
        try:
            from rank_bm25 import BM25Okapi

            docs_tokens = [_tokenize_for_bm25(c.text) for c in chunks]
            query_tokens = _tokenize_for_bm25(query_text)
            if docs_tokens and query_tokens:
                bm25 = BM25Okapi(docs_tokens)
                raw_bm25 = bm25.get_scores(query_tokens)
                max_bm25 = max(raw_bm25) if len(raw_bm25) > 0 else 0.0
                if max_bm25 > 1e-6:
                    bm25_norm_scores = [float(s / max_bm25) for s in raw_bm25]
        except Exception as e:
            logger.debug("[retrieve_relevant_chunks] BM25 scoring failed, using dense only: %s", e)

    # 3. Hybrid fusion
    scored: List[Tuple[float, ResumeChunk, float, float]] = []
    for idx, chunk in enumerate(chunks):
        d_score = dense_scores[idx]
        k_score = bm25_norm_scores[idx]
        hybrid = (dense_weight * d_score) + (keyword_weight * k_score)
        scored.append((hybrid, chunk, d_score, k_score))

    scored.sort(key=lambda x: -x[0])
    selected = scored[:top_k]

    results: List[Dict[str, Any]] = []
    for hybrid, chunk, d_score, k_score in selected:
        results.append(
            {
                "id": chunk.id,
                "section": chunk.section,
                "text": chunk.text,
                "score": round(hybrid, 4),
                "dense_score": round(d_score, 4),
                "keyword_score": round(k_score, 4),
            }
        )

    return results


def format_retrieval_context(chunks: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    """
    Format retrieved chunks as clean markdown bullets for injection into LLM prompts.
    Matches the <retrieved_resume_bullets> contract in prompts.py.
    """
    if not chunks:
        return "(none)"

    lines: List[str] = []
    total_chars = 0

    for c in chunks:
        line = f"- {c['text']}"
        if total_chars + len(line) > max_chars:
            break
        lines.append(line)
        total_chars += len(line) + 1

    return "\n".join(lines) if lines else "(none)"
