"""
Local embeddings for job text using sentence-transformers and ONNX Runtime.
Hybrid: title-only vector + full vector (title + role-focused description).
Role-focused description strips boilerplate via heuristics (no LLM).

Optimizations (P1 & P2):
1. Pre-normalized unit L2 vectors (normalize_embeddings=True): reduces cosine similarity to pure dot product.
2. Length-sorted batching: groups texts by sequence length before model encoding to minimize self-attention padding tokens.
3. Content-hash embedding cache: caches candidate embeddings in Redis/Django cache by SHA-256 digest to avoid redundant inference on identical job postings.
4. BLAS-vectorized matrix scoring: evaluates candidate vs reference similarities via batch matrix multiplication without Python loops.
5. High-performance ONNX INT8 engine with automatic PyTorch SentenceTransformer fallback.
"""
import os
import re
import logging
import threading
import hashlib
from typing import List, Optional, Tuple, Sequence, Dict, Any

from django.conf import settings
from django.core.cache import cache

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

logger = logging.getLogger(__name__)

# Model configuration: 384 dimensions for all-MiniLM-L6-v2
_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Cache TTL for candidate text embeddings (default: 7 days)
EMBEDDING_CACHE_TTL = getattr(settings, "EMBEDDING_CACHE_TTL_SECONDS", 60 * 60 * 24 * 7)

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
    Uses JDCleanserService to strip boilerplate.
    """
    from .jd_cleanser import JDCleanserService

    return JDCleanserService.cleanse(description, title=title, max_chars=max_chars)


def split_into_sentences(text: str) -> List[str]:
    """Split text on sentence boundaries (. ! ? and newlines). Returns non-empty stripped strings."""
    if not (text or "").strip():
        return []
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


def get_role_sentences(title: str, description: str, max_role_chars: int = 2500) -> List[str]:
    """Role-focused text -> split into sentences -> drop boilerplate. Returns list of sentences to embed."""
    role_text = extract_role_description(description or "", title or "", max_chars=max_role_chars)
    if not role_text:
        return []
    sentences = split_into_sentences(role_text)
    return [s for s in sentences if not is_boilerplate_sentence(s)][:MAX_ROLE_SENTENCES]


def title_only_for_embedding(title: str, company_name: str = "") -> str:
    """Text for title-only vector: job title only (no company). Keeps semantic space for role, not named entities."""
    t = (title or "").strip()
    if t:
        return f"Job title: {t}."
    return ""


def full_text_for_embedding(title: str, description: str, max_role_chars: int = 2500) -> str:
    """Text for full vector: title + role-focused description slice."""
    t = (title or "").strip()
    role_slice = extract_role_description(description or "", t, max_chars=max_role_chars)
    if t and role_slice:
        return f"Job title: {t}. {t}. Role: {role_slice}"
    if t:
        return f"Job title: {t}. {t}."
    return role_slice or ""


def get_embedding_cache_key(text: str) -> str:
    """Deterministic content-hash cache key for an arbitrary text snippet."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"cand_emb_v1:{digest}"


# --- INFERENCE ENGINE (ONNX INT8 Runtime with PyTorch Fallback) ---

class EmbeddingEngine:
    """
    Optimized embedding inference engine supporting ONNX INT8 Runtime with
    transparent fallback to PyTorch SentenceTransformer.
    Ensures single-threaded execution to prevent worker CPU hogging,
    and returns unit L2-normalized 384-dimensional vectors.
    """
    def __init__(self):
        self.backend = getattr(settings, "EMBEDDING_BACKEND", "auto")  # 'auto', 'onnx', or 'torch'
        self.onnx_session = None
        self.tokenizer = None
        self.torch_model = None
        self.input_names = []
        self._init_model()

    def _init_model(self):
        # 1. Try ONNX INT8 quantized engine if 'auto' or 'onnx'
        if self.backend in ("auto", "onnx"):
            try:
                import onnxruntime as ort
                from transformers import AutoTokenizer
                from huggingface_hub import hf_hub_download

                tokenizer = AutoTokenizer.from_pretrained(f"sentence-transformers/{_MODEL_NAME}")
                onnx_path = None
                for fname in ("onnx/model_quint8_avx2.onnx", "onnx/model.onnx"):
                    try:
                        onnx_path = hf_hub_download(f"sentence-transformers/{_MODEL_NAME}", filename=fname)
                        break
                    except Exception:
                        continue

                if onnx_path:
                    opts = ort.SessionOptions()
                    opts.intra_op_num_threads = 1
                    opts.inter_op_num_threads = 1
                    session = ort.InferenceSession(onnx_path, opts, providers=["CPUExecutionProvider"])
                    self.tokenizer = tokenizer
                    self.onnx_session = session
                    self.input_names = [i.name for i in session.get_inputs()]
                    self.backend = "onnx"
                    logger.info("[embeddings] Initialized ONNX INT8 engine with model: %s", onnx_path)
                    return
            except Exception as e:
                logger.warning("[embeddings] Failed to initialize ONNX engine (%s), falling back to PyTorch", e)

        # 2. PyTorch fallback
        try:
            import torch
            torch.set_num_threads(1)
        except Exception:
            pass
        from sentence_transformers import SentenceTransformer
        try:
            self.torch_model = SentenceTransformer(_MODEL_NAME, local_files_only=True)
        except Exception:
            self.torch_model = SentenceTransformer(_MODEL_NAME)
        self.backend = "torch"
        logger.info("[embeddings] Initialized PyTorch SentenceTransformer engine")

    def encode_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        if not texts:
            return []
        import numpy as np

        if self.backend == "onnx" and self.onnx_session and self.tokenizer:
            all_vecs: List[List[float]] = []
            for i in range(0, len(texts), batch_size):
                chunk = texts[i : i + batch_size]
                inputs = self.tokenizer(chunk, padding=True, truncation=True, max_length=256, return_tensors="np")
                feed = {k: v.astype(np.int64) for k, v in inputs.items() if k in self.input_names}
                out = self.onnx_session.run(None, feed)[0]
                mask = np.expand_dims(feed["attention_mask"], -1)
                pooled = np.sum(out * mask, axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                final_vecs = pooled / np.maximum(norms, 1e-9)
                all_vecs.extend(final_vecs.tolist())
            return all_vecs

        # Fallback PyTorch with unit L2 normalization
        vecs = self.torch_model.encode(texts, batch_size=batch_size, normalize_embeddings=True, convert_to_numpy=True)
        return vecs.tolist()


_ENGINE: Optional[EmbeddingEngine] = None
_ENGINE_LOCK = threading.Lock()


def _get_engine() -> EmbeddingEngine:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = EmbeddingEngine()
    return _ENGINE


def _get_model():
    """Backwards compatibility helper: returns underlying PyTorch model or engine."""
    engine = _get_engine()
    if engine.torch_model is not None:
        return engine.torch_model
    return engine


# --- CACHED & LENGTH-SORTED BATCH ENCODER ---

def _encode_texts_cached(texts: Sequence[str]) -> List[Optional[List[float]]]:
    """
    Batched, length-sorted, and Redis/Django-cache accelerated text encoding.
    1. Checks cache for existing embeddings (by SHA-256 content hash).
    2. Groups uncached texts and sorts them by string length to minimize attention padding in mini-batches.
    3. Runs inference via ONNX INT8 or PyTorch with unit L2 normalization.
    4. Un-sorts and saves newly computed embeddings to cache in bulk.
    5. Returns unit-normalized 384-dimensional vectors matching the original input order.
    """
    if not texts:
        return []

    cleaned_texts = [(t or "").strip() for t in texts]
    keys = [get_embedding_cache_key(ct) if ct else None for ct in cleaned_texts]

    # 1. Bulk cache lookup
    valid_keys = [k for k in keys if k]
    cached_map: Dict[str, Any] = {}
    if valid_keys:
        try:
            cached_map = cache.get_many(valid_keys) or {}
        except Exception as e:
            logger.debug("[embeddings] Cache get_many failed: %s", e)

    out: List[Optional[List[float]]] = [None] * len(texts)
    uncached_indices: List[int] = []
    uncached_texts: List[str] = []

    for idx, (ct, key) in enumerate(zip(cleaned_texts, keys)):
        if not ct or not key:
            out[idx] = None
            continue
        cached_vec = cached_map.get(key)
        if cached_vec and isinstance(cached_vec, list) and len(cached_vec) == EMBEDDING_DIM:
            out[idx] = cached_vec
        else:
            uncached_indices.append(idx)
            uncached_texts.append(ct)

    # 2. Encode uncached texts with length-sorted batching
    if uncached_texts:
        try:
            engine = _get_engine()
            # Length-sorted batching: minimizes padding tokens across heterogeneous texts
            sorted_order = sorted(range(len(uncached_texts)), key=lambda i: len(uncached_texts[i]))
            sorted_texts = [uncached_texts[i] for i in sorted_order]

            encoded_sorted = engine.encode_batch(sorted_texts)

            # Restore original order of uncached items
            to_cache = {}
            for orig_pos, vec in zip(sorted_order, encoded_sorted):
                actual_idx = uncached_indices[orig_pos]
                out[actual_idx] = vec
                k = keys[actual_idx]
                if k and vec:
                    to_cache[k] = vec

            # 3. Bulk cache write
            if to_cache:
                try:
                    cache.set_many(to_cache, timeout=EMBEDDING_CACHE_TTL)
                except Exception as e:
                    logger.debug("[embeddings] Cache set_many failed: %s", e)
        except Exception as e:
            logger.warning("[embeddings] Batch encoding failed: %s", e)

    return out


# --- PUBLIC EMBEDDING APIS ---

def embed_title_only(title: str, company_name: str = "") -> Optional[List[float]]:
    """Embed title + company only. Returns 384-dim unit vector or None."""
    text = title_only_for_embedding(title, company_name)
    if not text:
        return None
    res = _encode_texts_cached([text])
    return res[0] if res else None


def embed_text(text: str, max_chars: int = 2000) -> Optional[List[float]]:
    """Embed arbitrary text (e.g. resume). Truncates to max_chars for model limit. Returns 384-dim unit vector or None."""
    if not (text or "").strip():
        return None
    t = (text or "").strip()[:max_chars] or " "
    res = _encode_texts_cached([t])
    return res[0] if res else None


def embed_full(title: str, description: str) -> Optional[List[float]]:
    """Embed title + role-focused description. Returns 384-dim unit vector or None."""
    text = full_text_for_embedding(title, description)
    if not text:
        return None
    res = _encode_texts_cached([text])
    return res[0] if res else None


def embed_sentences_batch(sentences: List[str]) -> List[Optional[List[float]]]:
    """Embed a list of sentence strings with caching and length-sorting. Returns list of vectors."""
    if not sentences:
        return []
    return _encode_texts_cached(sentences)


def embed_title_only_batch(items: List[Tuple[str, str]]) -> List[Optional[List[float]]]:
    """Batch embed (title, company_name) pairs. Returns list of vectors or None."""
    texts = [title_only_for_embedding(t, c) for t, c in items]
    return _encode_texts_cached(texts)


def embed_full_batch(items: List[Tuple[str, str]]) -> List[Optional[List[float]]]:
    """Batch embed (title, description) pairs using role-focused description. Returns list of vectors or None."""
    texts = [full_text_for_embedding(t, d) for t, d in items]
    return _encode_texts_cached(texts)


def embed_job_text(title: str, description: str) -> Optional[List[float]]:
    """Single full vector (title + role-focused description). Used for like-endpoint stored embedding."""
    return embed_full(title, description)


def embed_job_texts_batch(items: List[tuple]) -> List[Optional[List[float]]]:
    """Batch (title, description) -> full vectors. Kept for compatibility."""
    return embed_full_batch(items)


def get_role_sentence_vectors_batch(
    items: Sequence[Tuple[str, str]]
) -> List[List[Optional[List[float]]]]:
    """Batch helper: compute role-focused sentence embeddings per (title, description) item."""
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
    """Deprecated: kept only for backwards-compatibility in debug views."""
    sentences = get_role_sentences(title, description)
    if not sentences:
        return []
    vecs = embed_sentences_batch(sentences)
    return [v for v in vecs if v is not None]


def mean_vector(vectors: List[List[float]]) -> Optional[List[float]]:
    """Return centroid of a list of vectors, normalized to unit length."""
    if not vectors:
        return None
    try:
        import numpy as np

        arr = np.array(vectors, dtype=float)
        mean_arr = arr.mean(axis=0)
        norm = float(np.linalg.norm(mean_arr))
        if norm > 1e-9:
            mean_arr = mean_arr / norm
        return mean_arr.tolist()
    except Exception:
        return None


# --- VECTORIZED MATHEMATICAL SCORING (BLAS MATRIX ACCELERATION) ---

def cosine_similarity(vec_a: Optional[Sequence[float]], vec_b: Optional[Sequence[float]]) -> float:
    """
    Cosine similarity between two vectors.
    Optimized: when vectors are unit-normalized (norm ~ 1.0), simplifies to pure dot product np.dot(a, b).
    Falls back gracefully to norm division for un-normalized vectors.
    Returns value in [-1.0, 1.0].
    """
    if not vec_a or not vec_b:
        return 0.0
    import numpy as np

    a = np.asarray(vec_a, dtype=np.float32)
    b = np.asarray(vec_b, dtype=np.float32)
    dot = float(np.dot(a, b))
    norm_a_sq = float(np.dot(a, a))
    norm_b_sq = float(np.dot(b, b))
    if 0.98 <= norm_a_sq <= 1.02 and 0.98 <= norm_b_sq <= 1.02:
        return float(np.clip(dot, -1.0, 1.0))
    n = dot / (np.sqrt(norm_a_sq * norm_b_sq) + 1e-9)
    return float(np.clip(n, -1.0, 1.0))


def batch_max_similarity(
    candidate_vecs: Sequence[Optional[Sequence[float]]],
    reference_vecs: Sequence[Sequence[float]],
) -> List[float]:
    """
    Vectorized cosine similarity scoring via BLAS matrix multiplication.
    For each candidate vector, computes the maximum cosine similarity across all reference vectors.
    Replaces row-by-row nested Python loops with single (K, 384) @ (384, M) dot product.
    Returns a list of float max similarity scores in [-1.0, 1.0].
    """
    if not candidate_vecs or not reference_vecs:
        return [0.0] * len(candidate_vecs)
    import numpy as np

    # Filter and normalize reference vectors
    ref_list = [r for r in reference_vecs if r is not None and len(r) == EMBEDDING_DIM]
    if not ref_list:
        return [0.0] * len(candidate_vecs)

    ref_mat = np.asarray(ref_list, dtype=np.float32)  # shape (M, 384)
    ref_norms = np.linalg.norm(ref_mat, axis=1, keepdims=True)
    ref_mat = ref_mat / np.maximum(ref_norms, 1e-9)

    valid_indices = []
    valid_cand_list = []
    for idx, c in enumerate(candidate_vecs):
        if c is not None and len(c) == EMBEDDING_DIM:
            valid_indices.append(idx)
            valid_cand_list.append(c)

    results = [0.0] * len(candidate_vecs)
    if valid_cand_list:
        cand_mat = np.asarray(valid_cand_list, dtype=np.float32)  # shape (K, 384)
        cand_norms = np.linalg.norm(cand_mat, axis=1, keepdims=True)
        cand_mat = cand_mat / np.maximum(cand_norms, 1e-9)

        # Single BLAS matrix multiplication: (K, 384) @ (384, M) -> (K, M)
        sim_matrix = np.dot(cand_mat, ref_mat.T)
        max_sims = np.max(sim_matrix, axis=1)  # shape (K,)
        for orig_idx, max_sim in zip(valid_indices, max_sims):
            results[orig_idx] = float(np.clip(max_sim, -1.0, 1.0))

    return results


def role_similarity_topk_mean(
    role_sent_vecs: Optional[Sequence[Optional[List[float]]]],
    pref_role_sentences: Optional[Sequence],
    top_k: int,
) -> float:
    """
    Sentence-level similarity between a job's role sentences and preference role sentences.
    Vectorized with matrix multiplication across all sentence pairs.
    """
    import numpy as np

    try:
        if not role_sent_vecs or not pref_role_sentences:
            return 0.0

        pref = pref_role_sentences
        if isinstance(pref, Sequence) and pref and isinstance(pref[0], (int, float)):
            pref_vecs = [np.array(pref, dtype=np.float32)]
        else:
            pref_vecs = [
                np.array(v, dtype=np.float32)
                for v in pref
                if v is not None and len(v) == EMBEDDING_DIM
            ]
        if not pref_vecs:
            return 0.0

        job_vecs = [
            np.array(v, dtype=np.float32)
            for v in role_sent_vecs
            if v is not None and len(v) == EMBEDDING_DIM
        ]
        if not job_vecs:
            return 0.0

        pref_mat = np.stack(pref_vecs, axis=0)  # shape (P, 384)
        pref_norms = np.linalg.norm(pref_mat, axis=1, keepdims=True) + 1e-9
        pref_mat = pref_mat / pref_norms

        job_mat = np.stack(job_vecs, axis=0)    # shape (J, 384)
        job_norms = np.linalg.norm(job_mat, axis=1, keepdims=True) + 1e-9
        job_mat = job_mat / job_norms

        # Matrix multiply (J, 384) @ (384, P) -> (J, P)
        sim_matrix = np.dot(job_mat, pref_mat.T)
        max_sims = np.max(sim_matrix, axis=1)  # shape (J,)

        sorted_sims = sorted(max_sims.tolist(), reverse=True)
        k = max(1, min(top_k or 1, len(sorted_sims)))
        return float(np.mean(sorted_sims[:k]))
    except Exception as e:
        logger.warning("role_similarity_topk_mean failed, returning 0.0: %s", e)
        return 0.0
