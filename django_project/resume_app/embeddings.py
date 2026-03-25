"""
Local embeddings for job text using sentence-transformers.
Hybrid: title-only vector + full vector (title + role-focused description).
Role-focused description strips boilerplate via heuristics (no LLM).
"""
import re
import logging
from typing import List, Optional, Tuple, Sequence

logger = logging.getLogger(__name__)

# Lazy-loaded model; 384 dimensions by default.
# EMBEDDING_DIM can change if you swap models; ensure all stored vectors are
# recomputed or versioned when doing so.
_MODEL = None
_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Role vs fluff section headers (case-insensitive substrings)
ROLE_HEADER_KEYWORDS = (
    "job description", "description", "responsibilities", "qualifications",
    "requirements", "about the role", "overview", "you will", "what you'll do",
    "what you will", "role", "summary", "duties", "must have", "experience",
)
FLUFF_HEADER_KEYWORDS = (
    "why join", "benefits", "culture", "our team", "equal opportunity",
    "chart your journey", "deliver your impact", "about us", "about the company",
    "who we are", "life at", "perks", "compensation",
)
# Lead-in phrases to skip at start of description
LEADIN_FLUFF_PATTERNS = [
    r"^chart your journey[.!]?\s*",
    r"^why you should join[.!]?\s*",
    r"^at\s+[\w\s&]+\s*,?\s*(we\s+)?(our|both)\s+",
    r"^from a collaborative culture[.!]?\s*",
    r"^find a career that will grow[.!]?\s*",
    r"^deliver your impact[.!]?\s*",
]

# Boilerplate sentence substrings (case-insensitive); sentences containing these are dropped for role similarity
BOILERPLATE_SENTENCE_PHRASES = (
    "equal opportunity",
    "eeo",
    "affirmative action",
    "we offer competitive",
    "we offer a competitive",
    "join our team",
    "join us",
    "apply now",
    "background check",
    "drug screening",
    "must be able to",
    "ability to work",
    "diversity and inclusion",
    "inclusion and diversity",
    "we are an equal",
    "all qualified applicants",
    "reasonable accommodation",
    "without regard to",
    "race, color",
    "gender, race",
    "sexual orientation",
    "veteran status",
    "disability status",
    "chart your journey",
    "deliver your impact",
    "why you should join",
    "life at ",
    "our culture",
    "our values",
)
MIN_SENTENCE_LENGTH = 25  # skip very short fragments
MAX_ROLE_SENTENCES = 25   # cap per job to limit embed cost

def extract_role_description(description: str, title: str, max_chars: int = 800) -> str:
    """
    Extract role-focused slice of job description (responsibilities, requirements).
    Strips boilerplate (why join, benefits, culture). No LLM; heuristics only.
    """
    if not (description or "").strip():
        return ""
    d = (description or "").strip()
    title = (title or "").strip()

    # 1) Find section boundaries: **Header** or ## Header
    section_starts = list(re.finditer(r"(?:\*\*([^*]+)\*\*|^##\s*(.+?)(?:\n|$))", d, re.MULTILINE | re.IGNORECASE))
    if section_starts:
        collected = []
        for i, m in enumerate(section_starts):
            header = (m.group(1) or m.group(2) or "").strip().lower()
            if any(kw in header for kw in FLUFF_HEADER_KEYWORDS):
                break
            if any(kw in header for kw in ROLE_HEADER_KEYWORDS):
                start = m.end()
                end = section_starts[i + 1].start() if i + 1 < len(section_starts) else len(d)
                block = d[start:end].strip()
                if block:
                    collected.append(block)
        if collected:
            return " ".join(collected)[:max_chars]

    # 2) Lead-in skip: drop fluff at start, then take next max_chars
    rest = d
    for pat in LEADIN_FLUFF_PATTERNS:
        rest = re.sub(pat, "", rest, flags=re.IGNORECASE)
    role_markers = re.compile(
        r"(responsible for|you will|requirements?|qualifications?|the ideal candidate|about the role)",
        re.IGNORECASE,
    )
    if title and len(title) > 2:
        try:
            idx = rest.lower().index(title.lower())
            rest = rest[idx:]
        except ValueError:
            pass
    m = role_markers.search(rest)
    if m:
        rest = rest[m.start() : m.start() + max_chars]
    else:
        rest = rest[:max_chars]
    if rest.strip():
        return rest.strip()

    # 3) Fallback: first 2 paragraphs or first 600 chars
    paras = [p.strip() for p in d.split("\n\n") if p.strip()]
    if paras:
        return " ".join(paras[:2])[:max_chars]
    return d[:max_chars]


def split_into_sentences(text: str) -> List[str]:
    """Split text on sentence boundaries (. ! ? and newlines). Returns non-empty stripped strings."""
    if not (text or "").strip():
        return []
    # Split on . ! ? and newlines; keep segments
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    sentences = []
    for p in parts:
        s = (p or "").strip()
        if len(s) >= 10:  # skip tiny fragments from split
            sentences.append(s)
    return sentences[:MAX_ROLE_SENTENCES]


def is_boilerplate_sentence(sentence: str) -> bool:
    """True if sentence looks like generic HR/legal boilerplate."""
    if not sentence or len(sentence) < MIN_SENTENCE_LENGTH:
        return True
    lower = sentence.lower()
    return any(phrase in lower for phrase in BOILERPLATE_SENTENCE_PHRASES)


def get_role_sentences(title: str, description: str, max_role_chars: int = 800) -> List[str]:
    """Role-focused text -> split into sentences -> drop boilerplate. Returns list of sentences to embed."""
    role_text = extract_role_description(description or "", title or "", max_chars=max_role_chars)
    if not role_text:
        return []
    sentences = split_into_sentences(role_text)
    return [s for s in sentences if not is_boilerplate_sentence(s)][:MAX_ROLE_SENTENCES]


def embed_sentences_batch(sentences: List[str]) -> List[Optional[List[float]]]:
    """Embed a list of sentence strings. Returns list of vectors (or None for failures)."""
    if not sentences:
        return []
    # model.encode accepts list; use a single placeholder if empty to avoid empty batch
    texts = [s.strip() or " " for s in sentences]
    try:
        model = _get_model()
        vecs = model.encode(texts, convert_to_numpy=True)
        return [vecs[i].tolist() for i in range(len(texts))]
    except Exception as e:
        logger.warning("Batch embed sentences failed: %s", e)
        return [None] * len(sentences)


def get_role_sentence_vectors_batch(
    items: Sequence[Tuple[str, str]]
) -> List[List[Optional[List[float]]]]:
    """
    Batch helper: for each (title, description) pair, compute role-focused
    sentence embeddings.

    Returns a list (one entry per item); each entry is a list of vectors
    (or None for sentences that failed to embed).
    """
    results: List[List[Optional[List[float]]]] = []
    for title, description in items:
        sentences = get_role_sentences(title or "", description or "")
        if not sentences:
            results.append([])
            continue
        vecs = embed_sentences_batch(sentences)
        results.append(vecs)
    return results


def get_role_sentence_vectors(title: str, description: str) -> List[List[float]]:
    """
    Deprecated: kept only for backwards-compatibility in debug views.
    Prefer job-level embeddings via embed_job_text.
    """
    sentences = get_role_sentences(title, description)
    if not sentences:
        return []
    vecs = embed_sentences_batch(sentences)
    return [v for v in vecs if v is not None]


def mean_vector(vectors: List[List[float]]) -> Optional[List[float]]:
    """Return centroid of a list of vectors, or None if list is empty."""
    if not vectors:
        return None
    try:
        import numpy as np

        arr = np.array(vectors, dtype=float)
        return arr.mean(axis=0).tolist()
    except Exception:
        return None


def role_similarity_topk_mean(
    role_sent_vecs: Optional[Sequence[Optional[List[float]]]],
    pref_role_sentences: Optional[Sequence],
    top_k: int,
) -> float:
    """
    Sentence-level similarity between a job's role sentences and preference
    role sentences.

    - role_sent_vecs: list of vectors (one per sentence) for the job.
    - pref_role_sentences: either a list of vectors OR a single centroid
      vector; both shapes are supported.
    - top_k: compute mean of the top-k per-sentence max similarities.

    Returns cosine similarity in [-1, 1]. Falls back to 0.0 when inputs
    are missing or any error occurs.
    """
    import numpy as np

    try:
        if not role_sent_vecs:
            return 0.0
        if not pref_role_sentences:
            return 0.0

        # Normalise preference vectors: allow a single centroid vector (flat list)
        pref = pref_role_sentences
        if isinstance(pref, Sequence) and pref and isinstance(pref[0], (int, float)):
            pref_vecs = [np.array(pref, dtype=float)]
        else:
            pref_vecs = [
                np.array(v, dtype=float)
                for v in pref  # type: ignore[assignment]
                if v is not None
            ]
        if not pref_vecs:
            return 0.0

        job_vecs = [
            np.array(v, dtype=float)
            for v in role_sent_vecs
            if v is not None
        ]
        if not job_vecs:
            return 0.0

        pref_mat = np.stack(pref_vecs, axis=0)
        pref_norms = np.linalg.norm(pref_mat, axis=1) + 1e-9

        max_sims: List[float] = []
        for j_vec in job_vecs:
            j_norm = float(np.linalg.norm(j_vec) + 1e-9)
            sims = np.dot(pref_mat, j_vec) / (pref_norms * j_norm)
            max_sims.append(float(np.max(sims)))

        if not max_sims:
            return 0.0

        sorted_sims = sorted(max_sims, reverse=True)
        k = max(1, min(top_k or 1, len(sorted_sims)))
        return float(np.mean(sorted_sims[:k]))
    except Exception as e:
        logger.warning("role_similarity_topk_mean failed, returning 0.0: %s", e)
        return 0.0


def title_only_for_embedding(title: str, company_name: str = "") -> str:
    """Text for title-only vector: job title only (no company). Keeps semantic space for role, not named entities."""
    t = (title or "").strip()
    if t:
        return f"Job title: {t}."
    return ""


def full_text_for_embedding(title: str, description: str, max_role_chars: int = 800) -> str:
    """Text for full vector: title + role-focused description slice."""
    t = (title or "").strip()
    role_slice = extract_role_description(description or "", t, max_chars=max_role_chars)
    if t and role_slice:
        return f"Job title: {t}. {t}. Role: {role_slice}"
    if t:
        return f"Job title: {t}. {t}."
    return role_slice or ""


def _get_model():
    global _MODEL
    if _MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            _MODEL = SentenceTransformer(_MODEL_NAME)
        except Exception as e:
            logger.warning("Failed to load embedding model %s: %s", _MODEL_NAME, e)
            raise
    return _MODEL


def embed_title_only(title: str, company_name: str = "") -> Optional[List[float]]:
    """Embed title + company only. Returns 384-dim vector or None."""
    text = title_only_for_embedding(title, company_name)
    if not text:
        return None
    try:
        model = _get_model()
        vec = model.encode(text, convert_to_numpy=True)
        return vec.tolist()
    except Exception as e:
        logger.warning("Embed title_only failed: %s", e)
        return None


def embed_text(text: str, max_chars: int = 2000) -> Optional[List[float]]:
    """Embed arbitrary text (e.g. resume). Truncates to max_chars for model limit. Returns 384-dim vector or None."""
    if not (text or "").strip():
        return None
    t = (text or "").strip()[:max_chars] or " "
    try:
        model = _get_model()
        vec = model.encode(t, convert_to_numpy=True)
        return vec.tolist()
    except Exception as e:
        logger.warning("Embed text failed: %s", e)
        return None


def embed_full(title: str, description: str) -> Optional[List[float]]:
    """Embed title + role-focused description. Returns 384-dim vector or None."""
    text = full_text_for_embedding(title, description)
    if not text:
        return None
    try:
        model = _get_model()
        vec = model.encode(text, convert_to_numpy=True)
        return vec.tolist()
    except Exception as e:
        logger.warning("Embed full failed: %s", e)
        return None


def embed_title_only_batch(items: List[Tuple[str, str]]) -> List[Optional[List[float]]]:
    """Batch embed (title, company_name) pairs. Returns list of vectors or None."""
    texts = [title_only_for_embedding(t, c) for t, c in items]
    texts = [t or " " for t in texts]
    try:
        model = _get_model()
        vecs = model.encode(texts, convert_to_numpy=True)
        return [vecs[i].tolist() for i in range(len(texts))]
    except Exception as e:
        logger.warning("Batch embed title_only failed: %s", e)
        return [None] * len(items)


def embed_full_batch(items: List[Tuple[str, str]]) -> List[Optional[List[float]]]:
    """Batch embed (title, description) pairs using role-focused description. Returns list of vectors or None."""
    texts = [full_text_for_embedding(t, d) for t, d in items]
    texts = [t or " " for t in texts]
    try:
        model = _get_model()
        vecs = model.encode(texts, convert_to_numpy=True)
        return [vecs[i].tolist() for i in range(len(texts))]
    except Exception as e:
        logger.warning("Batch embed full failed: %s", e)
        return [None] * len(items)


def embed_job_text(title: str, description: str) -> Optional[List[float]]:
    """
    Single full vector (title + role-focused description). Used for like-endpoint stored embedding.
    """
    return embed_full(title, description)


def embed_job_texts_batch(items: List[tuple]) -> List[Optional[List[float]]]:
    """Batch (title, description) -> full vectors. Kept for compatibility."""
    return embed_full_batch(items)


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Cosine similarity between two vectors. Returns value in [-1, 1]."""
    import numpy as np
    a, b = np.array(vec_a, dtype=float), np.array(vec_b, dtype=float)
    n = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
    return float(n)
